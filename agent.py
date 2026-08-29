#!/usr/bin/env python3
import xml.etree.ElementTree as ET
# -*- coding: utf-8 -*-

"""
AEL-MINI AUTONOMOUS AGENT v162

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
import select
import shlex
import shutil
import hashlib
import difflib
import subprocess
import traceback
import uuid
import html
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse
from web_search import web_search
from datetime import datetime

# Opcjonalna, czysto-pythonowa biblioteka (żadnych skompilowanych
# zależności — bezpieczna na Termux, w odróżnieniu od np. pydantic,
# które wymaga komponentu w Rust) do naprawiania SKŁADNIOWO zepsutego
# JSON-a z LLM (brakujący cudzysłów, przecinek na końcu, niedomknięty
# nawias) — patrz parse_json() niżej. Import jest opcjonalny: jeśli
# pakiet nie jest jeszcze zainstalowany (`pip install json_repair`),
# parse_json() po prostu pomija tę dodatkową próbę i działa dokładnie
# tak jak wcześniej — nigdy nie wywraca programu.
try:
    from json_repair import repair_json as _repair_json
except ImportError:
    _repair_json = None



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
    import pychrome
except Exception as e:
    print("[KRYTYCZNY] Brak pychrome")
    print(e)
    print("pip install -U pychrome")
    sys.exit(1)


# Zaobserwowany realny efekt uboczny v146 (zamiana na pychrome,
# 2026-08-28): za każdym razem, gdy zamykamy połączenie
# (_PychromeConnection.close() -> tab.stop()), wątek _recv_loop
# pychrome.Tab (patrz pychrome/tab.py) czasem dostaje PUSTY string z
# właśnie zamykanego websocketu zamiast wyjątku WebSocketException —
# a pychrome łapie w tym wątku WYŁĄCZNIE WebSocketException/OSError,
# więc json.loads("") wywala się jako NIEZŁAPANY JSONDecodeError,
# którego domyślny excepthook Pythona wypisuje jako pełny traceback
# wprost na ekran użytkownika. Wynik każdego wywołania CDP w logach
# był mimo to zawsze poprawny (to WYŁĄCZNIE kosmetyczny, alarmująco
# wyglądający ślad wyścigu przy zamykaniu, nie realna usterka) — ale
# wygląda jak awaria i myli użytkownika co do stanu CDP. Filtrujemy
# TYLKO ten dokładny, znany przypadek (JSONDecodeError wewnątrz
# pychrome/tab.py:_recv_loop) — każdy inny wyjątek w dowolnym wątku
# leci dalej do domyślnego excepthooka, żeby nigdy nie ukryć
# prawdziwego błędu.
import threading as _threading_bootstrap

_default_threading_excepthook = _threading_bootstrap.excepthook


def _pychrome_recv_loop_excepthook(args):

    if args.exc_type is json.JSONDecodeError:

        tb = args.exc_traceback

        while tb is not None:

            frame = tb.tb_frame

            if (
                frame.f_code.co_name == "_recv_loop"
                and "pychrome" in frame.f_code.co_filename
            ):
                return

            tb = tb.tb_next

    _default_threading_excepthook(args)


_threading_bootstrap.excepthook = _pychrome_recv_loop_excepthook


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

# Opcjonalne DRUGIE konto DeepSeek — jeśli plik istnieje i zawiera
# token, część ról (patrz _ROLE_ACCOUNT niżej) używa go zamiast
# konta głównego, żeby rozłożyć 9 jednoczesnych sesji na dwa konta
# zamiast katować limity jednego. Jeśli plik nie istnieje/jest
# pusty, WSZYSTKO działa dokładnie tak jak wcześniej — jedno konto.
TOKEN_FILE_2 = HOME / "api_token_2.txt"

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

# Minimalny odstęp między KOLEJNYMI wiadomościami do DeepSeek —
# niezależnie od roli/sesji, bo to WSPÓLNE konto/chat.deepseek.com.
# Realny przypadek: użytkownik dostał na stronie DeepSeek "Messages
# too frequent. Try again later" — to nie jest to samo co "invalid
# message id" (obsłużone przez circuit breaker w deepseek()), tylko
# zwykły limit tempa wysyłania po stronie serwera. consult_team()
# i tak woła role sekwencyjnie (jedna na raz), ale bez wymuszonego
# odstępu odpowiedzi przychodzą tak szybko, jak DeepSeek zdąży
# odpowiedzieć — a to może wciąż być za szybko dla jego limitu.
DEEPSEEK_MIN_INTERVAL_SECONDS = float(
    os.environ.get(
        "DEEPSEEK_MIN_INTERVAL_SECONDS",
        "6"
    )
)

COMMAND_TIMEOUT = int(
    os.environ.get(
        "COMMAND_TIMEOUT",
        "120"
    )
)

# Zaobserwowany realny problem: subprocess.run(..., shell=True) na
# Termuksie domyślnie uruchamia /system/bin/sh (albo minimalny sh w
# $PREFIX/bin), którego WBUDOWANE `echo` NIE obsługuje flagi `-e` —
# `echo -e "a\nb"` wypisuje dosłownie "-e a\nb" (znak nowej linii
# jako dwa znaki, nie prawdziwe przejście do nowej linii). To
# rzeczywiście uszkodziło zapisywane pliki (dosłowny "-e " na
# początku treści, powodujący SyntaxError). Bash OBSŁUGUJE `-e` w
# swoim wbudowanym echo — Termux instaluje bash domyślnie, więc
# wymuszamy go jako interpreter powłoki zamiast pozostawiać to
# systemowemu /bin/sh. Jeśli bash nie jest dostępny (np. inne
# środowisko), subprocess.run po prostu użyje domyślnego /bin/sh —
# bez zmiany zachowania.
_SHELL_EXECUTABLE = shutil.which("bash")

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

# Prefiks resource-id systemowego paska statusu/nawigacji Androida —
# te elementy powtarzają się identycznie w KAŻDYM zrzucie UI niezależnie
# od aktywnej aplikacji, więc android_summary() je pomija (patrz
# _parse_hierarchy).
_ANDROID_SYSTEMUI_RESOURCE_PREFIX = "com.android.systemui:"

# Generyczne, puste, nieklikalne kontenery frameworka Android/
# AppCompat, które android_summary() pomija (patrz _parse_hierarchy)
# — te same nazwy wracają w niemal KAŻDEJ aplikacji, nie tylko w
# systemowym UI, bez żadnej treści/interaktywności.
_ANDROID_GENERIC_CONTAINER_IDS = {
    "action_bar_root",
    "content",
    "coordinator",
    "root_view",
    "view_pager",
    "decor_content_parent",
    "contentPanel",
    "customPanel",
    "frame",
    # Dopisane po analizie realnych logów z 2026-08-28: te węzły
    # przechodziły przez filtr i wypełniały PRAWIE CAŁY zrzut
    # android_state w każdym kroku, wypychając poza limit znaków tę
    # część, która faktycznie dotyczyła celu.
    #
    # "0_resource_name_obfuscated" to nie jest prawdziwe id, tylko
    # zastępnik wstawiany, gdy id jest zaciemnione — w logach
    # pojawiał się po 3-6 razy w KAŻDYM zrzucie, zawsze bez tekstu i
    # bez możliwości kliknięcia.
    #
    # parentPanel/inputArea/topPanel/buttonPanel/customPanel to
    # standardowe kontenery AlertDialog z frameworka Androida (te
    # same na każdym urządzeniu i w każdym języku — filtr pozostaje
    # uniwersalny, nie zależy od polskich napisów), a
    # keyboard_holder/seeding_capsule_container_root to kontenery
    # klawiatury ekranowej i powłoki OEM.
    #
    # WAŻNE: wszystkie te wpisy i tak są pomijane WYŁĄCZNIE wtedy,
    # gdy węzeł nie ma własnego tekstu ani opisu I NIE jest
    # klikalny/fokusowalny (warunek niżej, niezmieniony) — jeśli
    # którykolwiek kiedyś będzie niósł realną treść, zostanie
    # pokazany.
    "0_resource_name_obfuscated",
    "parentPanel",
    "topPanel",
    "buttonPanel",
    "inputArea",
    "keyboard_holder",
    "seeding_capsule_container_root",
}

# Po ilu identycznych zadaniach (na poziomie decyzji MAIN)
# wymuszamy zmianę strategii.
REPEAT_LIMIT = 3

# Po ilu identycznych porażkach TEGO SAMEGO narzędzia z TYMI
# SAMYMI argumentami wywołujemy automatycznie CODE_REVIEWER/
# CODE_FIXER zamiast czekać, aż MAIN sam na to wpadnie.
# Celowo niższe niż REPEAT_LIMIT — to sygnał bardziej precyzyjny
# (dotyczy konkretnego wywołania, nie tylko podobnej treści TASK-u).
TOOL_REPEAT_LIMIT = 2

# Po ilu porażkach Z RZĘDU TEGO SAMEGO narzędzia — ale za KAŻDYM
# razem z INNYMI argumentami (dlatego TOOL_REPEAT_LIMIT/CODE_REVIEWER
# nigdy się nie uruchamia — sygnatura (tool, arguments) jest za
# każdym razem inna) — podpowiadamy PLANNEROWI/ENGINEEROWI, żeby
# rozważyli napisanie realnego narzędzia w custom_tools/ zamiast
# kolejnej pojedynczej komendy powłoki. Zaobserwowany realny
# przypadek: kolejne, coraz to inne warianty `grep`/`strings` na
# tym samym pliku kontaktów, żadne nie identyczne, więc licznik
# (tool, arguments) zawsze zaczynał liczyć od nowa.
GENERIC_TOOL_FAILURE_STREAK_LIMIT = 3

# ============================================================
# NOWE ŚCIEŻKI / PLIKI (naprawa + weryfikacja)
# ============================================================

# Ustrukturyzowany log (JSON Lines) — obok czytelnego dla
# człowieka log(). Jedna linia = jedno zdarzenie.
EVENTS_LOG_FILE = AGENT_DIR / "agent_events.jsonl"

# Trwały licznik powtarzających się porażek tego samego
# (narzędzie, argumenty) — przeżywa restart agenta.
TOOL_ATTEMPTS_FILE = STATE_DIR / "tool_attempts.json"

# Trwały licznik porażek Z RZĘDU TEGO SAMEGO narzędzia,
# NIEZALEŻNIE od dokładnych argumentów (patrz
# GENERIC_TOOL_FAILURE_STREAK_LIMIT powyżej).
TOOL_FAILURE_STREAK_FILE = STATE_DIR / "tool_failure_streak.json"

# Flaga: czy agent w TEJ sesji już próbował poszukać danych kontaktu
# (numeru telefonu) bezpośrednio na telefonie (termux-contact-list),
# zanim ewentualnie zapyta o to użytkownika — patrz
# _decision_asks_for_contact_info()/_mark_contacts_lookup_attempted()
# przy run_agent(). Kasowana razem z resztą stanu sesji w
# maybe_clear_previous_session_data(), żeby nowy cel nie dziedziczył
# "sprawdzone" po zupełnie niepowiązanym poprzednim celu.
CONTACTS_LOOKUP_ATTEMPTED_FILE = STATE_DIR / "contacts_lookup_attempted.flag"

# Wartość wklejona przez użytkownika w odpowiedzi na NEED_USER_LOGIN
# (klucz API, token, kod) — zapisana pod STAŁĄ, znaną ścieżką.
#
# ZAOBSERWOWANY REALNY, STRUKTURALNY BRAK (log 2026-08-28, cel
# "zadzwoń do Beaty", KROKI 11-13): użytkownik wkleił klucz API Bland,
# ENGINEER faktycznie go zobaczył (redakcja z v135 działa: dosłowna
# wartość trafia wyłącznie do niego) i MAIN też go widzi — ale GEMINI,
# czyli ten, kto realnie pisze i uruchamia skrypt, NIE MA ŻADNEGO
# kanału, żeby tę wartość dostać. Gemini dostaje wyłącznie tekst TASK-u
# od MAIN-a. W efekcie Gemini napisało skrypt bez klucza i dostało
# `401 {"error":"AUTH_FAILURE","message":"No Authorization Header or
# Session Cookie"}`, po czym zaczęło szukać poświadczeń po plikach na
# dysku — i znalazło jedynie atrapę z ~/agent/.env po starej sesji
# (klucz "pk-mock-valid-key-for-tests").
#
# Zapis pod stałą ścieżkę zamyka tę lukę bez przepychania sekretu
# przez prozę modeli: skrypt czyta wartość z pliku, a w treści TASK-u
# wystarczy ŚCIEŻKA, nie sama wartość. Plik dostaje prawa 0600 i jest
# kasowany razem z resztą stanu sesji (maybe_clear_previous_session_data),
# żeby nie stał się dokładnie tym, co właśnie naprawiliśmy — nieaktualną
# wartością z poprzedniego uruchomienia.
USER_PROVIDED_VALUE_FILE = STATE_DIR / "user_provided_value.txt"

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

# Rejestr punktów (TASK-ów) bieżącego celu z ich statusem —
# ZWERYFIKOWANY (Python sam potwierdził dowód, nie tylko deklarację
# Gemini), ZADEKLAROWANY_BEZ_DOWODU, BLAD, W_TOKU. Patrz
# _checklist_add()/_checklist_record_result()/_checklist_summary_block()
# przy create_task()/run_next_task() i w konsultacji zespołu.
PROGRESS_CHECKLIST_FILE = AGENT_DIR / "progress_checklist.json"

# Rejestr PODEJŚĆ (zewnętrznych usług/serwisów), których zespół już
# próbował, wraz z tym, jak się skończyły.
#
# ZAOBSERWOWANY REALNY PROBLEM (log 2026-08-28, cel "zadzwoń do
# Beaty"): zespół krążył między usługami — Twilio -> Bland -> Vapi ->
# Ainora -> z powrotem Bland — a POSTĘP CELU skakał 25% -> 10% ->
# 20% -> 10%. To nie był błąd estymatora, tylko wierne odbicie tego
# błądzenia. Przyczyna jest strukturalna: checklist śledzi TASK-i, a
# nie PODEJŚCIA, więc nigdzie nie było zapisane "Twilio odpadło, bo
# nie mamy poświadczeń", "Ainora odpadła, bo nie ma publicznego API".
# Zespół co kilka kroków wracał do czegoś, co już odrzucił, bo nikt
# nie pamiętał DLACZEGO to odrzucono — każdy krok z osobna był
# sensowny, całość była błądzeniem.
#
# Rejestr budowany jest DETERMINISTYCZNIE przez Pythona (hosty z
# adresów URL w treści zadania i w argumentach narzędzi), bez ani
# jednego dodatkowego zapytania do DeepSeeka i bez polegania na tym,
# że któraś rola sama o tym pamięta.
APPROACHES_FILE = AGENT_DIR / "approaches.json"

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

# Gemini regularnie próbuje sam przetestować dopiero co napisane
# narzędzie przez "python -c 'from agent.custom_tools.X import run'".
# POPRAWKA v74: ta wcześniejsza wersja komentarza błędnie zakładała,
# że winny jest tylko brak __init__.py — realny log z 2026-08-23
# pokazał, że błąd "'agent' is not a package" wystąpił MIMO że te
# pliki już istniały. Prawdziwa przyczyna: proces działa z katalogu
# ~/agent jako CWD (patrz `~/agent $ python3 agent.py`), więc `-c`
# dodaje CWD do sys.path — a ~/agent/agent.py (PLIK, sam bot) leży
# TAM, gdzie Python szuka pakietu "agent". Trafia więc najpierw na
# ten plik jako zwykły MODUŁ i już nigdy nie sprawdza ~/agent jako
# KATALOGU-pakietu (do czego __init__.py w ogóle by się przydał) —
# stąd "'agent' is not a package", niezależnie od __init__.py.
# Ten import ZAWSZE zawiedzie z tego katalogu, więc od v74 jest
# strukturalnie blokowany w execute_shell() (patrz
# _CUSTOM_TOOL_SELF_TEST_IMPORT_PATTERN) zamiast liczyć na to, że
# Gemini zapamięta, żeby go nie próbować. __init__.py zostają mimo
# to na miejscu — nieszkodliwe i przydałyby się, gdyby kiedyś CWD
# było inne.
try:
    for _pkg_dir in (AGENT_DIR, CUSTOM_TOOLS_DIR):
        _init_file = _pkg_dir / "__init__.py"
        if not _init_file.exists():
            _init_file.write_text("")
except Exception:
    pass

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
#
# Zaobserwowany realny przypadek: brakowało "pkg install" (mimo że
# "apt install" już tu było) — a to WŁAŚNIE "pkg" jest natywnym,
# najczęściej używanym w Termuxie poleceniem instalacji pakietów
# (apt jest pod spodem, ale ENGINEER prawie zawsze pisze "pkg
# install"). Efekt: "pkg install ..." leciało SYNCHRONICZNIE i
# ginęło na COMMAND_TIMEOUT (returncode 124) w trakcie testowania
# mirrorów Termuksa, zamiast automatycznie przejść w tło.
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
    "pkg install",
    "pkg upgrade",
    "pkg update",
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
    TOKEN_FILE.name,
    TOKEN_FILE_2.name,
    ".termux",
    ".termux_run_command_scripts",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".shortcuts",
}

# Zaobserwowany realny problem: $HOME/agent (AGENT_DIR) to JEDNOCZEŚNIE
# katalog programu agenta (agent.py, state/, queue/, results/,
# custom_tools/, current_goal.txt — nigdy nie do usunięcia) I
# sankcjonowana lokalizacja dla plików-DOWODÓW wygenerowanych PRZEZ CEL
# (patrz verify_final() — AGENT_DIR / "FINAL_OK.txt" to jedna z 3
# akceptowanych ścieżek). Wcześniej CAŁY katalog "agent" był wykluczony
# z śledzenia jednym wpisem w _PROJECT_TRACKING_EXCLUDED_NAMES — więc
# pliki takie jak ~/agent/FINAL_OK.txt czy ~/agent/rozmowa_z_beata.txt
# NIGDY nie trafiały do PROJECT_DIRS_FILE i przetrwały KAŻDE 'wyczysc'
# bezterminowo, zaśmiecając kolejne, niepowiązane cele (dokładnie
# przypadek z loga: "FINAL_OK.txt był pozostałością po niepowiązanym
# zadaniu" — plik istniał od dawna, bo cleanup nigdy go nie widział).
# Naprawa: wewnątrz AGENT_DIR patrzymy o JEDEN poziom głębiej — chronimy
# tylko KONKRETNE, znane pliki/katalogi programu, a wszystko inne (np.
# luźne pliki-dowody zapisane bezpośrednio w ~/agent/) faktycznie
# śledzimy do sprzątnięcia.
_AGENT_DIR_PROTECTED_SECOND_LEVEL_NAMES = {
    STATE_DIR.name,
    QUEUE_DIR.name,
    RESULTS_DIR.name,
    MEMORY_DIR.name,
    CUSTOM_TOOLS_DIR.name,
    APK_OUTPUT_DIR.name,
    GEMINI_KEYS_DIR.name,
    GEMINI_KEY_FILE.name,
    LAST_RESULT_FILE.name,
    EVENTS_LOG_FILE.name,
    GOAL_FILE.name,
    ADB_CONNECT_FILE.name,
    PROJECT_DIRS_FILE.name,
    PROGRESS_CHECKLIST_FILE.name,
    "screenshots",
}


def _track_project_path(path):
    """
    Zapisuje top-level katalog/plik pod $HOME (albo, dla ścieżek
    wewnątrz $HOME/agent, plik/katalog jeden poziom głębiej — patrz
    _AGENT_DIR_PROTECTED_SECOND_LEVEL_NAMES powyżej), w którym Gemini
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

        if top_level_name == AGENT_DIR.name:

            if len(relative.parts) < 2:
                # Sam katalog "agent" (nie powinno się zdarzyć w
                # praktyce) — nic konkretnego do śledzenia.
                return

            second_level_name = relative.parts[1]

            if (
                second_level_name
                in _AGENT_DIR_PROTECTED_SECOND_LEVEL_NAMES
            ):
                return

            if second_level_name.startswith("."):
                return

            if second_level_name.startswith("agent.py"):
                # Sam skrypt agenta i jego kopie zapasowe
                # (agent.py.bak_YYYYMMDD_HHMMSS z CODE_FIXERA).
                return

            if second_level_name == "web_search.py":
                # Zaobserwowany realny problem (skan $HOME,
                # 2026-08-26): web_search.py leży obok agent.py w
                # AGENT_DIR i jest TWARDĄ zależnością (moduł
                # importowany na starcie: "import web_search") —
                # bez niego program w ogóle się nie uruchomi. Mimo
                # to NIE był chroniony jak agent.py, więc gdyby
                # kiedykolwiek został nadpisany przez
                # write_engineer_code_to/termux_write_file (np.
                # CODE_FIXER "naprawiający" go), trafiłby do
                # PROJECT_DIRS_FILE i mógłby zostać pokazany do
                # usunięcia w zwykłym sprzątaniu — jedno "t" bez
                # wczytania się w listę i program przestałby się
                # uruchamiać.
                return

            top_level_path = str(AGENT_DIR / second_level_name)

        else:

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
    print("             AEL-MINI AUTONOMOUS AGENT v162")
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

# Które role wysyłają przez konto 2 (jeśli TOKEN_FILE_2 istnieje)
# zamiast konto 1.
#
# WAŻNE: podział NIE jest po liczbie ról (4 vs 5) — to była pierwsza,
# błędna wersja. consult_team() pyta role z bardzo różną
# częstotliwością:
#   - MAIN, PLANNER, CRITIC, ENGINEER — KAŻDY krok
#     (waga ~1.0 każda),
#   - RESEARCHER — co ~3. krok + świeży błąd narzędzia (waga ~0.4),
#   - BROWSER — co ~3. krok (waga ~0.33),
#   - PROGRESS_ESTIMATOR — co 5. krok (waga ~0.2),
#   - CODE_REVIEWER/CODE_FIXER — tylko przy powtarzającym się
#     błędzie, rzadko (waga ~0.05 każda).
# Wrzucenie WSZYSTKICH czterech "co krok" ról na jedno konto (jak
# w pierwszej wersji) zostawiało to konto z ~4.0 wagi ruchu, a
# drugie z ~1.0 — podział tylko z nazwy, nie z realnego obciążenia.
# Poniższy podział rozdziela też "co krok" role między oba konta,
# żeby faktycznie wyrównać ruch (~2.6 vs ~2.4 wagi):
_ROLE_ACCOUNT = {
    "MAIN": 1,
    "CRITIC": 1,
    "RESEARCHER": 1,
    "PROGRESS_ESTIMATOR": 1,
    "WOJTEK": 1,

    "PLANNER": 2,
    "ENGINEER": 2,
    "BROWSER": 2,
    "CODE_REVIEWER": 2,
    "CODE_FIXER": 2,
}

# {1: token_konta_1, 2: token_konta_2_lub_1_jesli_brak_drugiego}
_account_tokens = {}


def _activate_account_for_role(name):
    """
    Przełącza globalny opendeep.config.api_key na token właściwy
    dla roli `name`, TUŻ PRZED wysłaniem/utworzeniem sesji.

    Bezpieczne wyłącznie dlatego, że wszystkie wywołania do
    DeepSeek w tym programie są sekwencyjne (jedno na raz — patrz
    consult_team() i blokady w _get_session_lock) — opendeep trzyma
    api_key w jednym globalnym obiekcie config czytanym na żywo
    przy KAŻDYM żądaniu (nie zapisuje go w instancji sesji przy
    tworzeniu), więc bez tego przełączania nie dałoby się w ogóle
    rozróżnić kont w jednym procesie.
    """

    account = _ROLE_ACCOUNT.get(name, 1)

    token = (
        _account_tokens.get(account)
        or _account_tokens.get(1)
    )

    if token:
        opendeep.configure(api_key=token)


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

    token2 = read_text(
        TOKEN_FILE_2
    ).strip()

    _account_tokens[1] = token
    _account_tokens[2] = token2 or token

    if token2:
        log(
            "DEEPSEEK",
            "Drugie konto wykryte (" + TOKEN_FILE_2.name + ") — "
            "role " + ", ".join(
                r for r, a in _ROLE_ACCOUNT.items() if a == 2
            ) + " będą go używać."
        )

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

W kontekście dostajesz linię "PUNKTY ZADAŃ" — zwięzły, sprawdzony
PRZEZ PYTHON (nie przez deklarację Gemini) rejestr dotychczasowych
TASK-ów. "zweryfikowany dowodem" = Python sam potwierdził to
niezależnie — plik na dysku ISTNIEJE, ALBO w trakcie zadania
android_assert_text_visible faktycznie zwrócił found=true dla
tekstu wymienionego w warunku sukcesu (nie sama proza raportu).
"zadeklarowany BEZ dowodu" = Gemini NAPISAŁO że zrobione, ale
NIKT tego niezależnie nie sprawdził — traktuj to jako niepewne, NIE
jako fakt. Jeżeli spróbujesz zlecić TASK identyczny z już
zweryfikowanym punktem, zostanie automatycznie odrzucony ze statusem
TASK_DUPLICATE_OF_VERIFIED_POINT — to nie błąd do naprawienia, to
sygnał żeby zaproponować NASTĘPNY, inny krok.

Jeżeli PUNKTY ZADAŃ pokazuje sekcję "NIEDOKOŃCZONE (błąd, WYMAGAJĄ
PONOWIENIA)" — to punkty z CELU, które zawiodły i NIGDY nie zostały
skończone. Zaobserwowany realny problem: zespół po porażce jednego
punktu (np. "otwórz kalkulator i potwierdź wynik") po prostu szedł
dalej do kolejnych punktów celu, a porzucony punkt nigdy nie wracał
— pod koniec sesji cel wyglądał na "prawie gotowy", mimo że jeden z
jego wymaganych kroków nigdy się nie wykonał. PRIORYTETOWO ZLEĆ
PONOWNĄ PRÓBĘ takiego punktu (innym podejściem niż to, co zawiodło),
ZANIM przejdziesz do zupełnie nowej, jeszcze nietkniętej części celu
— chyba że dany punkt jest już od dawna, wielokrotnie niemożliwy do
wykonania i lepiej to zgłosić użytkownikowi niż próbować w
nieskończoność.

TASK ma być całym logicznym blokiem, nie mikro-krokiem typu
"kliknij X" — raczej w stylu: "Sprawdź stronę X, przejdź przez
cały proces, wykonaj konieczne działania i potwierdź wynik."

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

ENGINEER to Twój specjalista TECHNICZNY od budowy DOWOLNEGO
projektu — gry, aplikacji Android, skryptu, narzędzia CLI,
automatyzacji, strony itd., zależnie od aktualnego CELU. Poniższe
zasady dotyczą jego kodu niezależnie od rodzaju projektu.

TWÓJ OBOWIĄZEK w konkretnej sytuacji, nie opcja do rozważenia:
jeżeli ENGINEER podał w swojej odpowiedzi
gotowy blok kodu (wewnątrz ```...```),
A Twój TASK dotyczy ZAPISANIA tego kodu do pliku — MASZ UŻYĆ pola
"write_engineer_code_to" z docelową ścieżką
(np. "/data/data/com.termux/files/home/game3d/game.py"). Python
zapisze kod do pliku SAM, zanim TASK w ogóle trafi do Gemini — zero
zużycia Gemini na przepisywanie. Pole "task" ma wtedy dotyczyć
WYŁĄCZNIE uruchomienia i przetestowania już zapisanego pliku (np.
"Uruchom ~/game3d/game.py i sprawdź czy proces nie kończy się
błędem") — plik już tam będzie, zanim Gemini zacznie pracować.

ZABRONIONE w tej sytuacji: opisywanie kodu słownie w "task" i
liczenie, że Gemini sam go napisze/odtworzy przez termux_write_file.
To marnuje limit Gemini (którego brakuje) i wprowadza błędy
przepisywania — dokładnie to, czego ten mechanizm ma unikać. Jeżeli
zauważysz, że ostatni TASK kazał Gemini samodzielnie napisać duży
plik, mimo że ENGINEER miał gotowy kod — to był błąd,
napraw podejście w następnym TASKu.

Jeżeli w ostatniej odpowiedzi ENGINEER NIE MA bloku
kodu (```...```), pole zostanie odrzucone z jasnym błędem — nie
zgaduj, poproś ENGINEER o konkretny kod albo zrób
zwykły TASK bez tego pola (dozwolone tylko gdy naprawdę nie ma
gotowego kodu do zapisania).

============================================================
UWAGA — write_engineer_code_to NADPISUJE CAŁY PLIK
============================================================

write_engineer_code_to zastępuje CAŁĄ zawartość pliku blokiem kodu
ENGINEER — bezpieczne TYLKO gdy ten blok to PEŁNA, kompletna
zawartość pliku (np. pierwszy zapis nowego pliku, albo ENGINEER
świadomie podał cały plik od nowa).

Dla POPRAWKI FRAGMENTU istniejącego pliku (np. "zmień tę jedną
funkcję") użyj zamiast tego zwykłego TASKu instruującego Gemini, żeby
użyło termux_patch_file (search/replace) — poproś Gemini, żeby
najpierw odczytało plik (termux_read_file), znalazło dokładny
fragment do zmiany, i podmieniło go przez termux_patch_file. To jest
właściwe narzędzie do poprawek fragmentów; write_engineer_code_to
jest do zapisu całych plików — jeżeli ENGINEER podał tylko fragment,
a użyjesz write_engineer_code_to, nadpiszesz nim resztę pliku i
zniszczysz wszystko inne.
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

UWAGA: SecurityException/"Permission Denial" NIE jest sam w sobie
powodem do FAILED — patrz niżej sekcja o `adb shell pm grant`, samo
uprawnienie zazwyczaj da się nadać bez użytkownika.

============================================================
NEED_USER_LOGIN — zamiast FAILED, gdy blokerem jest coś, co
TYLKO CZŁOWIEK potrafi zrobić w przeglądarce
============================================================

Zaobserwowany realny przypadek: cel wymagał usługi zewnętrznej
(konto, logowanie, 2FA, CAPTCHA, zgoda na uprawnienia konta) — Gemini
nie potrafi tego wpisać ani ominąć, więc jedyną drogą było FAILED.
To NIEPOTRZEBNE — zamiast się poddawać, gdy JEDYNYM realnym
blokerem jest krok wymagający ręcznego kliknięcia/zalogowania się
przez człowieka na stronie (a NIE brak pomysłu na rozwiązanie ani
błąd w kodzie), zwróć:

{
  "type": "NEED_USER_LOGIN",
  "reason": "krótkie wyjaśnienie po co ta strona",
  "url": "pełny adres http(s) strony do otwarcia — TYLKO prawdziwa strona internetowa, patrz UWAGA niżej",
  "instructions": "co dokładnie użytkownik ma tam zrobić, np. 'Zaloguj się i przejdź do zakładki API Keys'"
}

Python otworzy ten adres w Chrome i zaczeka, aż użytkownik ręcznie
dokończy tam potrzebną czynność (login/rejestracja/2FA/zgoda) i
potwierdzi to na telefonie — TY dostaniesz o tym informację w
kolejnym kroku i będziesz mógł kontynuować, korzystając z już
zalogowanej karty Chrome (przez BROWSER/CDP) albo z danych, które
użytkownik stamtąd przekaże.

UWAGA — "url" MUSI być prawdziwym adresem http(s) strony internetowej
(np. usługi zewnętrznej, do której trzeba się zalogować). Zaobserwowany
realny błąd: MAIN podał "android://settings/apps/com.termux/
permissions" dla czynności w Ustawieniach SYSTEMOWYCH Androida (nie w
przeglądarce) — to NIE jest adres strony, Chrome nic sensownego z tym
nie zrobi (zostanie na pustej karcie), a "sukces" otwarcia był pozorny.
Jeżeli potrzebna czynność dotyczy Ustawień Androida/systemu, a NIE
strony w przeglądarce — zostaw "url" puste ("") i opisz DOKŁADNĄ ścieżkę
menu w "instructions" (np. "Ustawienia -> Aplikacje -> Termux ->
Uprawnienia -> włącz Mikrofon") — użytkownik i tak zrobi to ręcznie na
telefonie, bez otwierania Chrome.

Użyj tego TYLKO gdy naprawdę chodzi o czynność, którą fizycznie musi
kliknąć/wpisać człowiek (login, kod SMS, CAPTCHA, zgoda) — NIE jako
sposób na uniknięcie normalnej pracy, którą Gemini może wykonać samo
przez Termux/Android/Chrome.

KONKRETNY, zaobserwowany realny błąd (2026-08-27): zespół próbował
utworzyć asystenta w panelu Vapi przez zgadywane wywołania API
(POST/GET na wymyślony endpoint), a gdy to nie zadziałało — ZAMIAST
kliknąć widoczny na już otwartej, zalogowanej stronie przycisk
"Create Assistant" i wypełnić prosty formularz (nazwa, rozwijane
listy modelu/głosu) przez chrome_click/chrome_type/chrome_execute_js
— poprosił o to użytkownika. To jest DOKŁADNIE ta "normalna praca",
której NIE WOLNO przerzucać na człowieka: wypełnienie formularza na
stronie, na której Gemini i tak już jest zalogowane, to zwykłe
kliknięcia i wpisywanie tekstu, niezależnie od tego, ile pól ma
formularz. Zanim zwrócisz NEED_USER_LOGIN z powodu "nie udało się
przez API" — sprawdź NAJPIERW, czy tej samej czynności nie da się
po prostu wykonać przez UI (chrome_click na widoczny przycisk),
zamiast zakładać że trzeba zgadywać nieznane API albo pytać
człowieka.

KONKRETNY, zaobserwowany realny błąd (2026-08-27): zespół wcześniej w
tej samej sesji sam znalazł potrzebne dane bezpośrednio na telefonie
(np. numer kontaktu przez `termux-contact-list`, jeśli uprawnienie
READ_CONTACTS jest nadane) — a w kolejnym kroku, zamiast spróbować
tego samego narzędzia jeszcze raz, MAIN od razu napisał "dane nie są
dostępne w plikach systemowych" i poprosił o nie użytkownika przez
NEED_USER_LOGIN. To ten sam błąd co wyżej: zanim poprosisz człowieka
o dane, które mogą już istnieć NA TYM TELEFONIE (kontakt, zapisane
hasło, wcześniej pobrany plik) — spróbuj NAJPIERW znaleźć je
narzędziami, które już masz (np. `termux-contact-list` dla
kontaktów), zamiast od razu zakładać że trzeba pytać.

KONKRETNY, zaobserwowany realny błąd (2026-08-28): po tym, jak
użytkownik zalogował się do panelu Twilio na prośbę NEED_USER_LOGIN,
karta Chrome pokazywała `https://.../account/AC18e2f65e69db8b12fae...`
— Account SID (wartość zaczynająca się od "AC") był WIDOCZNY WPROST w
adresie URL już otwartej karty. Zespół tego nie sprawdził — zamiast
przeczytać adres otwartej karty (albo doczytać resztę danych z
zalogowanej strony przez chrome_execute_js/chrome_click), wymyślał
kolejne sposoby na ręczne wklejanie WSZYSTKICH danych przez
użytkownika (skrypt z `read`, zmienne środowiskowe w linii poleceń),
aż w końcu poddał się (FAILED), mimo że część odpowiedzi leżała
dosłownie w adresie karty, którą i tak już miał w swoim stanie Chrome.
PO KAŻDYM NEED_USER_LOGIN, ZANIM poprosisz o kolejną porcję danych —
sprawdź aktualny stan Chrome (adres URL, tytuł karty) i rozważ, czy
odpowiedź (albo jej część) nie jest już tam widoczna, zamiast zakładać
że wszystko musi przyjść ręcznie od człowieka.

KONKRETNY, zaobserwowany realny błąd (2026-08-28): gdy sesja
przeglądarki do panelu Twilio wygasła (automatyczne wyciągnięcie
Auth Token zwróciło pusty DOM), MAIN — ZAMIAST NEED_USER_LOGIN —
zwrócił zwykły TASK z poleceniem, żeby Gemini zapytał użytkownika o
token wprost w Termux (m.in. przez `read -s`). To NIE MOGŁO
zadziałać: Gemini nie ma ŻADNEGO narzędzia do prawdziwej,
zasygnalizowanej rozmowy z człowiekiem w czasie rzeczywistym — jego
komendy w Termux są wykonywane jako zwykłe polecenia powłoki, bez
mechanizmu, który przekazałby wpisaną przez człowieka wartość z
powrotem do last_result. Efekt: task zakończył się fałszywym
COMPLETED z pustym/nieprawidłowym tokenem, a właściwa prośba do
człowieka nigdy realnie nie dotarła — dwa kroki zespołu zmarnowane,
zanim ktoś to zauważył. JEDYNYM sposobem na uzyskanie wartości, którą
fizycznie musi wpisać/wkleić człowiek (hasło, token, kod z
dokumentu), jest NEED_USER_LOGIN — blokuje na prawdziwym wejściu na
poziomie Pythona i poprawnie przechwytuje to, co użytkownik wpisze.
NIGDY nie zlecaj tego jako TASK z instrukcją w stylu "zapytaj
użytkownika"/"poproś o wpisanie" wykonywaną przez Gemini w Termux —
to gwarantowana porażka, niezależnie jak sformułujesz polecenie.

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
- android_screenshot (prawdziwy zrzut ekranu PNG — OPCJONALNE,
  ostateczność: nikt — ani Gemini, ani zespół DeepSeek — nie
  odczytuje TREŚCI tego obrazu, więc plik sam w sobie potwierdza
  tylko "narzędzie się wykonało", nie "na ekranie jest to, co
  powinno być". Używaj TYLKO gdy naprawdę nie ma żadnego
  tekstowego sposobu potwierdzenia — np. czy własna grafika/gra
  (SurfaceView/OpenGL/Canvas) faktycznie coś rysuje, czego
  android_state (tekstowy dump drzewa UI) nie pokaże. Dla zwykłego
  "czy aplikacja X jest otwarta" android_state W ZUPEŁNOŚCI
  wystarcza i jest darmowy — NIE rób zrzutu ekranu tylko po to,
  żeby potwierdzić, że apka się otworzyła). Zrzuty są automatycznie
  kasowane po zakończeniu programu — nie traktuj ich jako trwałego
  zapisu, tylko jednorazowy dowód.
- android_screenshot_ocr (zrzut ekranu + NATYCHMIASTOWE rozpoznanie
  widocznego tekstu lokalnie przez Tesseract, ZERO kosztu limitu —
  działa offline, nikt nic nie wysyła do żadnego modelu. Zwraca
  sam TEKST, nie obraz. Używaj wyłącznie tam, gdzie android_state
  NIE pokazuje treści — np. tekst wyrenderowany wewnątrz WebView/
  Canvas/gry, który nie ma odpowiadającego węzła accessibility.
  Nadal NIE pomoże przy weryfikacji czystej grafiki bez tekstu
  — kształtów, kolorów, układu bez napisów)
- android_launch_app (otwórz DOWOLNĄ zainstalowaną aplikację po
  nazwie pakietu — np. zbudowaną i zainstalowaną grę, żeby ją
  faktycznie zobaczyć na ekranie, a nie tylko sprawdzić plik .apk)

ZAOBSERWOWANY REALNY, POWTARZAJĄCY SIĘ PROBLEM przy SZUKANIU nazwy
pakietu: gołe `pm list packages` w skrypcie bash (nawet gdy Gemini
PAMIĘTA dodać `adb shell` — a w praktyce, wielokrotnie, ZAPOMINAŁO)
może NIE ZNALEŹĆ aplikacji, która FAKTYCZNIE jest zainstalowana i
widoczna na ekranie użytkownika — np. Kalkulatora czy Zegara.
Przyczyna: od Androida 11 obowiązuje "package visibility" — zwykła
aplikacja (czym jest tu Termux) domyślnie widzi w PackageManagerze
tylko ograniczony zestaw innych pakietów. Ponieważ poleganie na
pamiętaniu o `adb shell` w RĘCZNIE pisanym skrypcie okazało się
NIEWYSTARCZAJĄCE — użyj zamiast tego bezpośrednio narzędzia
`android_list_packages(filter_text)` — ono ZAWSZE, strukturalnie,
przechodzi przez ADB, więc nie da się tu zapomnieć o niczym. Zawsze
używaj TEGO narzędzia (nie pisz własnego `pm list packages` w
skrypcie) do szukania nazwy pakietu PRZED android_launch_app.
- android_run_in_new_window (uruchom komendę w NOWYM, widocznym
  oknie Termuksa — konkretna komenda, nie puste okno jak
  termux_start_second_session; do procesów, które mają być
  widoczne osobno od głównego logu, np. serwer albo długi build)

ZAOBSERWOWANY REALNY PRZYPADEK — SecurityException/"Permission
Denial" (np. ACTION_CALL wymaga CALL_PHONE, termux-contact-list/
content query wymaga READ_CONTACTS): MAIN zwrócił FAILED z
uzasadnieniem "wymaga ręcznej interwencji użytkownika: nadania
uprawnień". To NIEPRAWDA — agent ma już połączenie ADB do TEGO
SAMEGO urządzenia (to samo, którego używają narzędzia android_*).
To połączenie pozwala SAMEMU nadać dowolnej aplikacji (Termux,
Termux:API) standardowe uprawnienie w tle, bez żadnego dialogu i
bez użytkownika:

  adb shell pm grant <nazwa.pakietu> android.permission.NAZWA

np. `adb shell pm grant com.termux.api android.permission.READ_CONTACTS`
albo `... android.permission.CALL_PHONE`. ZANIM zwrócisz FAILED z
powodu SecurityException/"Permission Denial" DLA JAKIEJKOLWIEK
aplikacji, zleć NAJPIERW zwykłym TASKiem (shell/termux_run) próbę
nadania brakującego uprawnienia tą komendą, a dopiero potem
ponowienie tej samej operacji. Dopiero jeśli samo `pm grant`
zwróci błąd (uprawnienie nie istnieje / jest uprawnieniem
specjalnym typu "signature", którego adb shell nie może nadać) —
to jest faktyczna, nie do obejścia bariera i FAILED jest zasadny.

ZASTRZEŻENIE (zaobserwowane na realnym urządzeniu, ROM ColorOS/
Realme): `pm grant` przez adb shell MOŻE zwrócić
`java.lang.SecurityException: ... Neither user 2000 nor current
process has android.permission.GRANT_RUNTIME_PERMISSIONS` — to
NIE jest błąd konkretnego uprawnienia ani literówka w nazwie,
tylko blokada na poziomie CAŁEGO urządzenia/ROM-u: adb shell na
tym telefonie w ogóle nie ma prawa nadawać ŻADNYCH uprawnień
(często wymaga osobnego przełącznika w Opcjach dewelopera, np.
"Debugowanie USB (ustawienia zabezpieczeń)" na ColorOS, którego
Gemini/DeepSeek NIE może sam włączyć). Jeśli zobaczysz DOKŁADNIE
ten komunikat ("GRANT_RUNTIME_PERMISSIONS") — NIE próbuj `pm
grant` ponownie dla INNYCH nazw uprawnień w tym samym celu, to
tylko powtórzy tę samą porażkę.

Zanim jednak uznasz to za niemożliwe do obejścia bez człowieka —
spróbuj JESZCZE JEDNEGO podejścia, które nie wymaga adb shell w
ogóle: zleć Gemini wywołanie narzędzia potrzebującego tego
uprawnienia WPROST (np. termux-contact-list, ACTION_CALL), bez
wcześniejszego `pm grant`. Jeśli Android jeszcze nie podjął decyzji
o tym uprawnieniu, system SAM pokaże natywne okienko z pytaniem o
zgodę na ekranie telefonu — Gemini może je wykryć przez
android_state i potwierdzić jednym kliknięciem (android_click na
widoczny przycisk "Zezwól"/"Allow"), co jest dużo mniejszą
przeszkodą dla użytkownika niż ręczne przejście przez Ustawienia >
Aplikacje > Uprawnienia. Dopiero jeśli po tym wywołaniu WCIĄŻ nie
ma żadnego okienka (uprawnienie zostało wcześniej trwale odrzucone
— Android wtedy nie pyta drugi raz) ani dostępu do funkcji, to jest
faktyczna, nie do obejścia bariera — zwróć NEED_USER_LOGIN (nie
FAILED — to fizyczna czynność, którą tylko człowiek może wykonać,
patrz sekcja NEED_USER_LOGIN wyżej) z jasnym opisem: wymaga to
ręcznego przełącznika w Opcjach dewelopera lub ręcznego nadania
uprawnienia w Ustawieniach > Aplikacje > (nazwa aplikacji) >
Uprawnienia.

CHROME:
- chrome_tabs
- chrome_inspect
- chrome_open
- chrome_click
- chrome_type
- chrome_execute_js (dowolny JavaScript w karcie, np. fetch() do
  API strony z realną sesją/ciasteczkami — użyj tego zamiast pisać
  taki kod do pliku, którego i tak nic nie uruchomi)

UWAGA — otwieranie URL do SPRAWDZENIA (tytuł/URL/zawartość):
w tym wdrożeniu Chrome/CDP działa w trybie "ISTNIEJĄCE KARTY" —
chrome_open NIE tworzy nowych kart, zadziała tylko gdy pasująca
karta już jest otwarta (inaczej zwróci błąd "Nie znaleziono
istniejącej karty. Nowe karty są zablokowane."). Jeśli żadna
pasująca karta nie istnieje, NIE próbuj wymuszać chrome_open w
kółko — zamiast tego zweryfikowany, działający sposób na
otworzenie i sprawdzenie NOWEGO adresu to: `am start -a
android.intent.action.VIEW -p com.android.chrome -d <url>` żeby
faktycznie wyświetlić stronę na ekranie W CHROME (KRYTYCZNE: zawsze
z `-p com.android.chrome` — bez tego Android może otworzyć adres w
INNEJ zainstalowanej przeglądarce, np. Firefoksie, co jest
zaobserwowanym realnym incydentem na tym urządzeniu), `curl -s <url> | grep -o '<title>.*</title>'`
(albo podobne parsowanie HTML) do sprawdzenia tytułu/zawartości
BEZ polegania na CDP/UI. android_state (stan ekranu telefonu jako
tekst) i chrome_tabs/chrome_inspect (stan ISTNIEJĄCEJ karty Chrome
jako tekst) to JEDYNE i WYSTARCZAJĄCE dowody dla "co jest na
ekranie/w przeglądarce" — są już częścią kontekstu każdego kroku,
nic nie kosztują i, co ważniejsze, TY (Gemini) faktycznie je
czytasz i na ich podstawie działasz.

KRYTYCZNE — potwierdzanie KONKRETNEGO wyniku/wartości (np. "wynik
działania to 19", "ekran pokazuje Zapisano"): NIE czytaj w tym celu
całego android_state i nie oceniaj "na oko", czy to tam jest —
zaobserwowany realny problem: takie potwierdzenia w raportach
("wynik 19 potwierdzony przez android_state") okazywały się SAMĄ
DEKLARACJĄ po pobieżnym przejrzeniu długiego zrzutu, nie faktycznym
sprawdzeniem. Użyj zamiast tego android_assert_text_visible("19") —
zwraca jednoznaczne found=true/false, więc Twoje potwierdzenie jest
FAKTYCZNYM dowodem, nie interpretacją.

android_screenshot NIE jest
tu potrzebny — to obraz PNG, którego treści nikt (ani Ty, ani
zespół DeepSeek) nie analizuje, więc sam plik potwierdza tylko
"narzędzie się wykonało", a nie "to, co powinno być widoczne,
faktycznie tam jest". Rób zrzut ekranu WYŁĄCZNIE gdy cel wprost
wymaga potwierdzenia własnej grafiki/gry (SurfaceView/OpenGL/
Canvas), której android_state nie pokaże w ogóle — nie rób go
rutynowo "na wszelki wypadek" przy każdym otwarciu apki/karty.

ZAOBSERWOWANY I POTWIERDZONY REALNY PROBLEM — ZNALEZIONA PRZYCZYNA:
goły `am start -a VIEW -d <url>` BEZ `-p com.android.chrome` pozwala
Androidowi wybrać DOWOLNĄ zainstalowaną aplikację obsługującą ten
intent — na tym urządzeniu otworzył Firefoksa zamiast Chrome. CDP
jest podłączone WYŁĄCZNIE do Chrome (adb forward tcp:9222), więc
karta otwarta w innej przeglądarce jest dla `chrome_tabs()`
całkowicie niewidoczna, niezależnie od tego, ile razy spróbujesz —
to nie jest kwestia czekania dłużej ani ponawiania. ZAWSZE dodawaj
`-p com.android.chrome`. Jeśli mimo to (np. inny wariant Chrome na
danym urządzeniu) po `am start` + kilku sekundach `chrome_tabs`/
`chrome_inspect` DALEJ nie pokazuje nowego URL — NIE próbuj tego w
kółko. Zamiast tego użyj `android_screenshot_ocr` (darmowe, lokalne,
zero limitu) — jeśli w rozpoznanym tekście jest nazwa/treść
oczekiwanej strony (np. "Wikipedia"), to WYSTARCZAJĄCY dowód, że
strona faktycznie się otworzyła, niezależnie od tego, co pokazuje
(albo jaka aplikacja obsłużyła) CDP.

Jeżeli mimo to potrzebujesz `dumpsys`/`uiautomator dump` z poziomu
skryptu bash: NIGDY nie uruchamiaj ich gołych w Termuksie —
zwrócą "not found"/odmowę dostępu, bo (dokładnie jak screencap)
to systemowe polecenia wymagające uprawnień użytkownika `shell`,
których zwykła aplikacja (czym jest tu Termux) nie ma. Jedyna
działająca droga to przepuszczenie ich przez ADB, tak samo jak
przy zrzucie ekranu:

  adb shell dumpsys activity activities | grep mResumedActivity
  adb shell dumpsys window windows | grep mCurrentFocus

To działa (uprzywilejowany `shell` przez ADB), podczas gdy gołe
`dumpsys ...` uruchomione wprost w Termuksie zawsze zawiedzie —
ale i tak PIERWSZY WYBÓR to android_state/chrome_tabs, bo są
prostsze i już dostępne bez dodatkowego polecenia.

KRYTYCZNE — gdy TASK wymaga dowodu wizualnego/stanu Chrome, treść
TASKu ma wprost nakazywać Gemini wywołanie KONKRETNEGO narzędzia
bezpośrednio ("wywołaj android_screenshot", "wywołaj chrome_tabs")
jako osobny krok w tym samym zadaniu. Nigdy nie każ mu wsadzać
android_screenshot / chrome_tabs / chrome_inspect / chrome_open /
android_launch_app DO treści skryptu bash (termux_run/
termux_run_background) — to narzędzia PO STRONIE GEMINI, nie
polecenia powłoki, więc taki skrypt po cichu nic z nimi nie zrobi,
a mimo to może zgłosić COMPLETED.

SHELL:
- shell

ZAOBSERWOWANY REALNY PROBLEM — `/tmp` W TERMUKSIE NIE DZIAŁA: to
katalog systemowy Androida (root filesystem), NIE `$PREFIX/tmp` —
zwykła aplikacja (czym jest Termux) nie ma do niego zapisu, próba
zapisu kończy się "Permission denied". Jeśli TASK potrzebuje pliku
tymczasowego, każ Gemini użyć `$HOME` (np. `~/tmp_cos.txt`, usunięty
na końcu skryptu) albo `$TMPDIR` — nigdy gołego `/tmp/...`.

Jeżeli zadanie wymaga katalogu, pliku, kodu, programu, instalacji,
serwera, gry, procesu, Termuxa, Androida czy Chrome — to zawsze
robota dla Gemini, nie dla Ciebie. Ty sam nie masz terminala i nie
musisz go mieć: to nie jest powód do FAILED, tylko sygnał, że
następny krok ma polegać na przekazaniu wykonania Gemini, które ma
zrobić cały logiczny blok zadania i samo sprawdzić rezultat.
Innymi słowy: brak terminala, brak execute_command/shell, brak
dostępu do plików czy możliwości utworzenia katalogu, zapisania
kodu albo uruchomienia programu — żadne z tego nie jest Twoim
ograniczeniem, bo to wszystko potrafi Gemini. FAILED zgłaszaj
tylko wtedy, gdy faktycznie nic — ani Ty, ani Gemini — nie jest w
stanie tego wykonać.

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
- Zapis narzędzia REJESTRUJE je NATYCHMIAST (już przy samym
  termux_write_file) — NIE każ Gemini "przetestować" go przez
  `python -c "from agent.custom_tools.X import run; ..."`. To
  strukturalnie ZABLOKOWANE i ZAWSZE zawiedzie: `~/agent/agent.py`
  (sam bot) leży dokładnie tam, gdzie Python szuka pakietu "agent",
  więc trafia na ten plik jako zwykły moduł, zanim w ogóle
  rozważyłby katalog jako pakiet — `ModuleNotFoundError: 'agent' is
  not a package` niezależnie od czegokolwiek. Żeby sprawdzić, czy
  działa, po prostu wywołaj je jako zwykłe narzędzie w NASTĘPNEJ
  konsultacji (pojawi się na liście dostępnych) — to jedyny sensowny
  test.

============================================================
ZADANIA POMIJAJĄCE BUDOWĘ OD ZERA
============================================================

Zadania próbujące pobrać/skopiować gotowe rozwiązanie zamiast
zbudować je samodzielnie (np. gotową grę, gotowy APK typu
Standoff 2, gotowy cudzy projekt jako całość) są automatycznie
blokowane, zanim trafią do Gemini — chyba że użytkownik w CELU
wyraźnie poprosił o zainstalowanie/użycie konkretnego istniejącego
narzędzia (to wtedy zgodność z celem, nie obejście blokady).
Domyślnie cel ma powstać od zera w Termuxie/Androidzie za
pośrednictwem Gemini, niezależnie czy to gra, aplikacja, skrypt
czy inne narzędzie — to samo dotyczy prób opisania tego samego
pomysłu innymi słowami.
============================================================
"""


PLANNER_PROMPT = """
Nazywasz się Tomek. W tym zespole zajmujesz się planowaniem
kolejnych kroków w realizacji DOWOLNEGO celu technicznego
zleconego przez użytkownika: może to być gra Android, zwykła
aplikacja Android, skrypt Pythona,
narzędzie CLI, automatyzacja, strona/serwer, scraper, cokolwiek
da się zbudować i uruchomić przez Termux, ADB/Android albo
Chrome. Rozpoznaj o co dokładnie chodzi z treści CELU, który
dostajesz w każdej wiadomości.

Dostajesz: cel, ostatni wynik, stan systemu. Wybierz JEDEN
konkretny, realistyczny następny krok — nie więcej niż 3 kroki
naprzód, każdy zrozumiały dla Gemini jako komenda Termux albo
konkretny plik do utworzenia. Buduj cel od zera w Termux/Android
przez Gemini — chyba że sam użytkownik w CELU wyraźnie poprosił o
użycie konkretnego istniejącego narzędzia (np. "zainstaluj
istniejące narzędzie X"), wtedy pomiń pobieranie/kopiowanie
gotowego rozwiązania (gotowej gry, gotowej apki, cudzego projektu).
Jeśli poprzedni krok skończył się timeoutem, rozbij podejście na
etapy (najpierw setup, potem build, potem sprawdzenie); jeśli krok
już dwa razy zawiódł tym samym sposobem, zaproponuj INNE podejście
— trzecia identyczna próba nic nie zmieni.

KRYTYCZNE — planuj TYLKO to, co realnie jeszcze nie działa/nie jest
potwierdzone: zanim zaplanujesz krok, sprawdź w OSTATNIM RAPORCIE/
historii, co z celu już zostało potwierdzone (pliki/dowody z
poprzednich zadań), i skieruj następny krok wyłącznie na brakującą
część, nie na cały cel od nowa. Jeśli tylko jeden podpunkt z kilku
wciąż zawodzi (np. zrzut ekranu), zaplanuj krok naprawiający TYLKO
ten jeden podpunkt. Zaobserwowany realny problem: cel miał 6
punktów, punkty 1,2,4,5 już dawno się udały (widać to w OSTATNIM
RAPORCIE/historii), a mimo to kolejne "NASTĘPNE KROKI" wciąż kazały
Gemini pisać jeden wielki skrypt na nowo wykonujący WSZYSTKIE 6
punktów od zera — marnowało to czas/wiadomości i wyglądało jak brak
postępu, choć większość była zrobiona.

KRYTYCZNE — zrzut ekranu (android_screenshot), sprawdzenie karty
Chrome (chrome_tabs/chrome_inspect/chrome_open) i otwarcie
aplikacji (android_launch_app) planuj jako OSOBNY, bezpośredni krok
dla Gemini ("wywołaj narzędzie android_screenshot bezpośrednio"),
NIE jako część jednego skryptu bash uruchamianego przez
termux_run/termux_run_background — to narzędzia po stronie Gemini,
nie polecenia shell; skrypt próbujący je zastąpić gołym
`screencap`/`dumpsys` po cichu zawiedzie mimo kodu wyjścia 0.
Czysty shell (mkdir, zapis pliku, curl) nadal możesz łączyć w jeden
skrypt, te konkretne narzędzia nie.

Odpowiadaj naturalnie, tak zwięźle jak się da — nie musisz za
każdym razem wypełniać identycznego szablonu punkt po punkcie.
Jeśli stan urządzenia jest oczywisty z poprzedniej rozmowy, nie
opisuj go od nowa, po prostu przejdź do planu. W odpowiedzi ma się
jednak zawsze dać jednoznacznie znaleźć: plan (max 3 kroki),
KONKRETNY następny krok (polecenie albo plik) i jak Gemini ma
sprawdzić, że ten krok się udał.
"""


RESEARCHER_PROMPT = """
Nazywasz się Kamil. W tym zespole zajmujesz się wyszukiwaniem
informacji, działając w Termux/Android. Cel bieżącego projektu
może być dowolny — gra,
aplikacja Android, skrypt, narzędzie CLI, automatyzacja, strona,
serwer, integracja z API — rozpoznaj go z treści CELU.

Twoja specjalizacja dopasowuje się do aktualnego celu: biblioteki i
frameworki dostępne w Termux dla danej technologii (pygame, kivy,
libgdx, cocos2d-x do gier; odpowiednie biblioteki Pythona, Javy czy
Node do innych zadań), błędy budowania (Android SDK/Gradle w
Termux, ale też pip/npm/kompilacja gdzie indziej) oraz komendy
apt/pip/npm/gradle działające bez roota w Termux.

NIE szukaj w internecie tego, JAK UŻYWAĆ WŁASNYCH NARZĘDZI tego
agenta (android_click, android_tap, chrome_tabs, termux_* itd.) —
to nie jest wiedza dostępna w internecie, tylko kwestia narzędzi,
które już masz opisane w Twoim własnym prompcie systemowym. Nikt w
sieci nie widział TEGO konkretnego ekranu, TEJ konkretnej aplikacji
ani TEGO narzędzia — wyszukiwanie typu "jak kliknąć przycisk w
Termuksie" zawsze zwróci albo nic, albo ogólniki, marnując
wiadomość na "myślenie" o czymś, co jest zwykłym wykonaniem, nie
prawdziwym problemem badawczym. Jeżeli kliknięcie/interakcja
zawodzi, to zadanie dla PLANNERA/ENGINEERA (inny tekst, inne
współrzędne, inne podejście na TYM ekranie) — nie dla wyszukiwarki.
WEB_SEARCH/WEB_FETCH zostaw na rzeczy faktycznie zewnętrzne: wersje
bibliotek, kompatybilność narzędzi, dokumentację API, treść
konkretnej strony wymienionej w celu.

Masz dwa sposoby na sprawdzenie czegoś w internecie SAMODZIELNIE
— bez pośrednictwa Gemini/telefonu, wykonuje je bezpośrednio Python
i od razu przekazuje Ci wynik w tej samej rozmowie:

- Gdy dopiero SZUKASZ informacji/rozwiązania i nie masz konkretnego
  adresu, wypisz jedną linię:
  WEB_SEARCH: <precyzyjne zapytanie po angielsku>
  Dostaniesz listę wyników (tytuły/linki/skróty), jak z wyszukiwarki.

- Gdy znasz już KONKRETNY adres strony i chcesz przeczytać jej
  treść (np. link z wyników wyszukiwania, dokumentacja, konkretna
  strona wspomniana w celu), wypisz jedną linię:
  WEB_FETCH: <pełny adres URL>
  Dostaniesz rzeczywistą treść tej strony (tekst, bez znaczników
  HTML) — nie zgaduj treści strony z samego adresu czy tytułu.

Odpowiadaj na podstawie tego, co faktycznie wiesz albo co zwróciło
wyszukiwanie/pobranie strony — jeśli czegoś nie jesteś pewien,
powiedz to wprost zamiast zgadywać. Jeśli właśnie dostałeś wynik,
nie proś o to samo jeszcze raz. Trzymaj odpowiedź krótką —
maksymalnie 5 zdań.
"""


CRITIC_PROMPT = """
Nazywasz się Marek. W tym zespole Twoja rola to złapać błędy
logiczne, zanim MAIN wyśle zadanie do Gemini — cel projektu może
być dowolny (gra Android, aplikacja, skrypt, narzędzie CLI,
automatyzacja, strona, serwer), rozpoznaj go z treści CELU.
Poniższe punkty warto sprawdzać za każdym razem:

- Czy ten sam krok nie był już wykonywany i kończył się timeoutem?
  Jeżeli tak — zaprotestuj i zaproponuj użycie termux_run_background
  lub podział na mniejsze kroki.
- Czy proponowany krok każe wykonać od zera coś, co poprzednie
  zadania już potwierdziły jako zrobione (np. cały skrypt na nowo
  wykonujący wszystkie podpunkty celu, gdy tylko JEDEN z nich
  faktycznie jeszcze zawodzi)? Zaobserwowany realny problem — to
  marnuje czas/wiadomości i wygląda jak brak postępu. Jeśli tak,
  zablokuj i zażądaj kroku dotyczącego wyłącznie tego, co jeszcze
  nie jest potwierdzone.
- Czy warunek sukcesu jest mierzalny (konkretny plik, exitcode 0,
  konkretny komunikat)?
- Czy zadanie nie jest za ogólne ("zrób grę"/"zrób program") —
  powinno być jeden konkretny krok.
- Czy nie próbujemy pobrać/skopiować gotowego rozwiązania zamiast
  je zbudować, skoro cel tego wymaga?
- Czy MAIN przypadkiem zmierza do DONE bez namacalnego dowodu
  właściwego dla tego konkretnego celu: dla gry/aplikacji Android
  — zbudowany i zainstalowany APK potwierdzony zrzutem ekranu; dla
  skryptu/narzędzia CLI — uruchomienie z oczekiwanym wynikiem lub
  kodem wyjścia 0; dla strony/serwera — potwierdzenie w
  przeglądarce/odpowiedź serwera. Sam plik/kod bez uruchomienia i
  dowodu działania to nie jest ukończenie celu.
- Sfabrykowane "potwierdzenia": porównaj konkretne liczby/fakty w
  OSTATNIM RAPORCIE z tym, co było w poprzednich krokach (jeśli
  masz dostęp do historii). Jeśli się różnią bez wyjaśnienia, albo
  RAPORT GEMINI (jego własna proza, nie STAN FAKTYCZNY) podaje
  "potwierdzone" fakty bez widocznego w tym kroku świeżego
  wywołania narzędzia, które by to faktycznie sprawdziło — zablokuj
  i zażądaj ponownego, rzeczywistego sprawdzenia (odczyt pliku /
  powtórzenie komendy), zanim to zostanie przyjęte jako dowód.
  Zaobserwowany realny przypadek — raport Gemini podał konkretne
  liczby jako rzekomo potwierdzone fakty (np. wersje narzędzi:
  "Python 3.11.8, Node v20.15.1, Gradle 8.9"), a kilka kroków
  wcześniej w tej samej rozmowie realnie odczytany plik pokazywał
  zupełnie inne liczby ("Python 3.14.6, Node v26.4.0, Gradle:
  brak") — Gemini nie sprawdziło niczego na nowo, wymyśliło
  wiarygodnie brzmiące dane zamiast zacytować to, co faktycznie
  wcześniej ustalono.
  WYJĄTEK — traktuj jako wystarczający dowód SAM W SOBIE (bez
  dodatkowego świeżego wywołania narzędzia) każdy fakt widoczny w
  bloku "STAN FAKTYCZNY" w kontekście: to wynik osobnego
  sprawdzenia PRZEZ PYTHON bezpośrednio na dysku w TYM kroku (patrz
  opis przy nim), nie deklaracja Gemini. Zaobserwowany realny
  problem — CRITIC blokował plan w kółko, żądając ponownego
  wywołania RESEARCHERA dla czegoś, co RESEARCHER już zrobił kroki
  wcześniej i co STAN FAKTYCZNY już potwierdzał — zespół nie miał
  jak tego kiedykolwiek "naprawić", bo żądanie było w istocie
  niespełnialne (powtórzenie czegoś, co już jest prawdą, nie
  wytworzy nowego dowodu silniejszego niż to, co Python już
  sprawdził).
- Zrzut ekranu / stan Chrome wsadzony do skryptu bash:
  android_screenshot, chrome_tabs/chrome_inspect/chrome_open i
  android_launch_app to narzędzia po stronie Gemini, nie polecenia
  shell — skrypt bash próbujący je zastąpić po cichu zawiedzie, a
  mimo to MAIN może dostać "COMPLETED". Jeśli proponowany krok
  wymaga zrzutu ekranu, stanu Chrome albo otwarcia aplikacji, a
  treść kroku każe to zrobić "w skrypcie" zamiast jako osobne,
  bezpośrednie wywołanie narzędzia Gemini — zablokuj i zażądaj
  rozbicia na osobny krok z bezpośrednim wywołaniem.
- Mylenie WŁASNEGO procesu agenta z celem/osobą z CELU: proces
  agenta NIGDY nie jest osobą/kontaktem/aplikacją z CELU, to Twój
  własny, uruchomiony program. Jeśli krok proponuje analizę
  procesu, którego PID/argv/cwd odpowiada plikowi `agent.py` —
  zablokuj i zażądaj podejścia skierowanego na właściwy cel
  (kontakty telefonu, zainstalowane aplikacje, dokumentacja
  narzędzia) zamiast dalszej analizy własnego procesu.
  Zaobserwowany realny przypadek — cel mówił o zadzwonieniu do
  konkretnej osoby, `ps aux | grep -i <imię>` nic nie znalazło, a
  zespół zaczął zamiast tego analizować `python3 agent.py` (czyli
  WŁASNY, aktualnie działający proces tego agenta — ten sam PID,
  który wykonuje ten cel) jako rzekomy "interfejs komunikacyjny"
  osoby z celu, aż w końcu odczytał treść WŁASNEGO kodu źródłowego
  (`/proc/<PID>/cwd/agent.py`) jako dowód — ślepy zaułek.
- REJESTRACJA MIĘDZY KROKAMI (czy dane z poprzedniego kroku faktycznie
  "trafiają" tam, gdzie kolejny krok ich potrzebuje — jak w druku
  warstwowym, gdzie każda warstwa musi się dokładnie pokryć z
  poprzednią, inaczej obraz "ucieka"): gdy plan Tomka każe użyć
  czegoś wyprodukowanego we wcześniejszym kroku (zapisany plik,
  wartość, zmienna) — sprawdź zarówno czy to coś istnieje, JAK I czy
  sposób jego UŻYCIA (dokładna nazwa, ścieżka, cudzysłowy/
  interpolacja) faktycznie odpowiada temu, jak to zostało zapisane.
  Jeśli nie masz pewności, zażądaj, żeby Bartek pokazał dokładne
  polecenie, zanim uznasz plan za gotowy do wykonania. Zaobserwowany
  realny przypadek — krok N zapisał klucz API do pliku, krok N+1
  miał go użyć w poleceniu `curl`, ale zmienna powłoki z tym kluczem
  była w POJEDYNCZYCH cudzysłowach (bash jej wtedy NIE podstawia) —
  obie warstwy z osobna wyglądały poprawnie, ale się nie
  "zarejestrowały", więc do zewnętrznego serwisu poleciał dosłowny
  tekst zmiennej zamiast jej wartości, a błąd (AUTH_FAILURE) wyszedł
  na jaw dopiero po fakcie.

Format:

OCENA: OK / OSTRZEŻENIE / BLOKUJ

PROBLEM (jeżeli OSTRZEŻENIE lub BLOKUJ):
...

POPRAWKA:
...
"""



CODE_REVIEWER_PROMPT = r"""
Nazywasz się Piotr. W tym zespole analizujesz kod, ale go nie
zmieniasz i nie nakładasz patcha samodzielnie; to zadanie
CODE_FIXERA na podstawie Twojej analizy.

Kiedy MAIN zgłosi błąd, przejdź przez rzeczywisty plik (nigdy nie
zgaduj jego struktury z pamięci): znajdź dokładne miejsce problemu,
sprawdź kontekst funkcji i jej zależności, rozważ czy to nie jest
skutek jakiegoś wcześniejszego patcha, i dopiero na tej podstawie
zaproponuj minimalną poprawkę. Jeśli danych, które dostałeś, na to
nie starcza — powiedz wprost, czego dokładnie potrzebujesz, zamiast
zgadywać.

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
Nazywasz się Ania. W tym zespole naprawiasz kod na podstawie
analizy CODE_REVIEWERA razem z dokładnym fragmentem istniejącego
kodu — pracuj wyłącznie na tym, co faktycznie dostałeś, nigdy na
domyślonej treści czy nazwach funkcji, których nie widziałeś.

Twoje zadanie to bezpieczna, jak najmniejsza poprawka — zmieniasz
tylko to, co konieczne, bez przepisywania całego programu i bez
zmiany architektury, chyba że MAIN wyraźnie o to poprosił. Samą
treść patcha piszesz Ty, ale backup, nałożenie, py_compile i
ewentualny rollback wykonuje kod agenta (apply_patch_from_fixer_
text) — dlatego odpowiedź musi trzymać się formatu poniżej co do
znaku, inaczej parser odrzuci nawet dobry pomysł. Fragment SZUKAJ
ma być skopiowany 1:1 z podanego kodu (te same wcięcia, te same
znaki) i występować w pliku dokładnie raz.

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
Nazywasz się Ola. Masz w tym zespole DWIE role.

============================================================
GŁÓWNA ROLA (na każdym kroku): TŁUMACZKA NA LUDZKI JĘZYK
============================================================

Reszta zespołu potrzebuje wiedzieć, co się wydarzyło w ostatnim
kroku — ale surowe dane (logi z Termuksa, wyniki narzędzi Gemini,
kody błędów) same w sobie nie są łatwe do szybkiego ogarnięcia.
Dostajesz taki surowy raport i CEL — Twoim zadaniem jest opowiedzieć
w 1-3 zdaniach, PO LUDZKU, co się właściwie stało, jakby po prostu
opowiadała koledze z zespołu, a nie odczytywała log. Bez żargonu i
nazw wewnętrznych narzędzi, chyba że są naprawdę potrzebne do
zrozumienia sensu. Nie oceniaj, nie planuj kolejnego kroku, nie
wydawaj opinii — tylko streść fakty jasno i po ludzku. Ta Twoja
odpowiedź trafia bezpośrednio do reszty zespołu jako opis ostatniego
kroku, więc ma być zrozumiała sama w sobie.

============================================================
DRUGA ROLA (tylko gdy dostaniesz stan Chrome): OCENA PRZEGLĄDARKI
============================================================

Gdy dostajesz też aktualny stan przeglądarki Chrome przez CDP (karty,
adresy, tytuły) — oceń, czy ma on sens względem celu, i jaki może być
sensowny następny krok w przeglądarce. To rola analityczna, nie
wykonawcza — oceniasz i sugerujesz, ale decyzje podejmuje MAIN, a
działania (w tym otwieranie kart) wykonuje Gemini.

Odpowiadaj krótko i konkretnie.
"""


ENGINEER_PROMPT = """
Nazywasz się Bartek. W tym zespole jesteś głównym inżynierem
technicznym, działającym w Termux/Android. Budujesz KAŻDY rodzaj
projektu, o
jaki poprosi użytkownik — grę Android, zwykłą aplikację Android,
skrypt Pythona, narzędzie CLI, automatyzację, scraper, serwer,
integrację z API, stronę itd. Rozpoznaj z treści CELU, jakiego
rodzaju projekt budujesz, i dostosuj do tego swoje rady — sekcje
poniżej dotyczące gier/APK/Gradle stosuj TYLKO gdy cel faktycznie
jest o budowie gry lub aplikacji Android; dla innych celów opieraj
się na ogólnej wiedzy inżynierskiej (Python, shell, biblioteki,
API, formaty plików itd.).

Dostajesz aktualny stan projektu i raport ostatniego zadania.

Twój jedyny cel: przygotować KONKRETNE, WYKONALNE polecenie lub
blok kodu, który Gemini może natychmiast uruchomić w Termux.

============================================================
TWOJA ROLA: KONKRETNY TECHNICZNY KROK, NIE WERDYKT O UKOŃCZENIU
============================================================

Dostarczaj KONKRETNY, TECHNICZNY następny krok. Oceną, czy CEL
(jako całość) jest osiągnięty, zajmują się CRITIC i MAIN na
podstawie fizycznych dowodów — to nie Twoja rola. Jeżeli uważasz,
że cel jest już zrealizowany, opisz DOKŁADNIE jaki dowód to
potwierdza i dlaczego uważasz go za wiarygodny; pisz "SUKCES"/"CEL
ZREALIZOWANY" jako gotową konkluzję TYLKO gdy sam masz TWARDY,
niezależnie sprawdzalny dowód (nie tylko własny wcześniejszy
skrypt, który to zadeklarował).

Zaobserwowany, wielokrotnie powtarzający się realny problem:
pisałeś "STATUS: SUKCES – CEL ZREALIZOWANY" na podstawie samego
istnienia pliku-dowodu (np. FINAL_OK.txt) albo raportu Gemini, mimo
że nikt niezależnie nie zweryfikował, czy faktycznie coś się
wydarzyło (np. czy połączenie/rozmowa naprawdę miały miejsce, a nie
tylko skrypt "powiedział", że tak). CRITIC to za każdym razem
poprawnie blokuje — ale to marnuje całą turę zespołu na coś, co
nigdy nie miało przejść.

============================================================
GDY MASZ NAPISAĆ NOWE NARZĘDZIE (custom_tools/)
============================================================

Jeśli plan wymaga stworzenia nowego, wielokrotnego użytku narzędzia
w ~/agent/custom_tools/<nazwa>.py — plik MUSI mieć DOKŁADNIE ten
kontrakt (inne nazwy pól/funkcji Python po cichu odrzuci, narzędzie
nigdy się nie zarejestruje):

TOOL_NAME = "nazwa_narzedzia"          # str
TOOL_DESCRIPTION = "Co robi."          # str
TOOL_PARAMETERS = {                    # JSON Schema, DOKŁADNIE ta nazwa
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"]
}

def run(x):                            # DOKŁADNIE ta nazwa funkcji
    return {"ok": True, ...}

Zaobserwowany realny problem: własne, "logiczne" nazwy jak
TOOL_PARAMS zamiast TOOL_PARAMETERS albo execute() zamiast run()
wyglądają poprawnie, ale są ciche odrzucane bez żadnego wyjątku w
raporcie Gemini — plik istnieje na dysku, ale nigdy nie trafia na
listę dostępnych narzędzi, a przyczyna nie jest nigdzie widoczna,
dopóki ktoś nie porówna nazw pole po polu. Plik musi być
samowystarczalny (własne importy na górze).

============================================================
KOMENDY termux-api MOGĄ ZWRÓCIĆ returncode 0 MIMO BŁĘDU
============================================================

Każdy skrypt, który wywołuje komendę termux-api zwracającą JSON
(termux-telephony-call, termux-camera-photo,
termux-microphone-record, termux-location, termux-contact-list
itd.), MUSI przechwycić jej wyjście do zmiennej i sprawdzić, czy
zawiera `"error"`, PRZED zadeklarowaniem sukcesu — np.:

RESULT=$(termux-telephony-call "$NUM")
if echo "$RESULT" | grep -q '"error"'; then
    echo "BLAD: $RESULT" > wynik_bledu.txt
    exit 1
fi

Deklaruj "SUKCES"/zapisuj plik-dowód TYLKO po takim sprawdzeniu
treści JSON — samo `&&`/kod wyjścia to za mało: te komendy
komunikują się z apką Termux:API przez IPC i zwracają kod wyjścia 0
po prostu za to, że DOSTAŁY odpowiedź, NIEZALEŻNIE od tego, czy ta
odpowiedź to sukces czy JSON z kluczem "error".

Zaobserwowany realny przypadek: skrypt wywołał
`termux-telephony-call "$NUM" && ... && echo SUKCES > wynik.txt`.
Cała komenda zwróciła `returncode: 0`, a mimo to
`termux-telephony-call` W OGÓLE nie zadzwonił — jego własny stdout
zawierał `{"error": "Please grant the following permission..."}`.
Wynik: plik z fałszywym "SUKCES" mimo realnego niepowodzenia,
złapane dopiero później przez inną rolę.

============================================================
WYCIĄGANIE TREŚCI Z HTML — UŻYWAJ PARSERA, NIE REGEXÓW
============================================================

Gdy zadanie wymaga wyciągnięcia treści z HTML (strona, dokumentacja,
wynik `curl`/`requests` na stronie WWW) — ZAWSZE proponuj najpierw
rozwiązanie przez parser HTML (np. `BeautifulSoup(html,
"html.parser").get_text()` albo `.find`/`.select` na konkretne
elementy), NIE przez `re.search`/`re.findall` na surowym HTML.
Parser HTML rozumie strukturę drzewa DOM i pozwala celować w
konkretne elementy (tag, klasa, selektor), zamiast zgadywać wzorzec
tekstowy — regex na HTML jest kruchy z założenia, więc trzymaj go w
zapasie jako ostateczność, na wypadek gdy parser faktycznie zawiedzie
na konkretnym, znanym powodzie, nie jako pierwsze podejście.

Zaobserwowany realny przypadek: zadanie wymagało wyciągnięcia
konkretnej treści (przykład kodu) ze strony dokumentacji Twilio.
Zaproponowałeś kolejno kilka podejść opartych o ręcznie pisane
wyrażenia regularne na surowym HTML tej strony — strona używała
Prism/Emotion do podświetlania składni, więc właściwy tekst był
porozbijany na dziesiątki zagnieżdżonych `<span>` z klasami CSS.
Efekt: 21 z 25 dostępnych wywołań narzędzi w tym kroku zostało
zużytych na kolejne regexy, które albo nie dawały żadnego
dopasowania, albo wyciągały fragment w złym języku, albo przepuszczały
surowy CSS/HTML zamiast czystego tekstu — każda zmiana zagnieżdżenia
znaczników łamała dopasowanie po cichu. Dopiero podejście przez
BeautifulSoup (już dostępny w środowisku Python użytkownika, bez
potrzeby instalacji) zadziałało poprawnie za pierwszym razem.

Trzymaj się wyłącznie budowy AKTUALNEGO projektu — bez marketingu,
grafiki marketingowej, dokumentacji czy sklepów. Każda Twoja
rekomendacja ma być konkretna: pełna komenda, pełna zawartość
pliku albo pełny fragment kodu do wklejenia, nie opis słowny.
Rozróżniaj etapy budowania i dostosuj je do rodzaju projektu
(poniżej przykład dla gry/apki Android, ten sam podział pasuje do
dowolnego projektu):
- setup środowiska (Python/Java/Gradle/Android SDK/venv/npm),
- struktura projektu (pliki, katalogi, manifest/konfiguracja),
- właściwy kod (logika, pętle, grafika — albo funkcje, endpointy,
  przetwarzanie danych, zależnie od celu),
- budowanie/uruchomienie (gradlew assembleDebug / python skrypt.py
  / npm start, zależnie od technologii),
- dla apek Android: podpisywanie i instalacja (adb install); dla
  innych projektów: właściwy dla nich dowód działania (kod
  wyjścia, plik wynikowy, odpowiedź HTTP itp.).

Jeśli poprzednie podejście skończyło się timeoutem, zaproponuj coś
lżejszego (mniejszy plik, mniej zależności) albo podziel build na
mniejsze kroki. Projekt ma powstać od zera w Termux, więc pomijaj
pobieranie gotowej gry, APK czy cudzego projektu — chyba że cel
wyraźnie prosi o użycie/zainstalowanie konkretnego istniejącego
narzędzia.

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
ZRZUT EKRANU / KARTA CHROME — ZAWSZE OSOBNE WYWOŁANIE NARZĘDZIA
(NIE WSADZAJ ICH DO SKRYPTU BASH)
============================================================

Jeśli TASK wymaga zrzutu ekranu lub sprawdzenia karty Chrome, te
dwa konkretne kroki rób jako OSOBNE wywołania narzędzi Gemini
(android_screenshot, chrome_tabs/chrome_inspect) — nigdy jako część
połączonego skryptu bash. Czysty shell (mkdir, zapis do pliku,
sprawdzanie wersji narzędzi, curl) nadal możesz łączyć w jeden
skrypt; zrzut ekranu i stan karty Chrome zostają na zewnątrz, jako
osobne kroki po skrypcie.

Powód: `android_screenshot` i `chrome_tabs`/`chrome_inspect` to
narzędzia PO STRONIE GEMINI/PYTHONA — nie mają odpowiednika jako
gołe polecenie shell. Termux, uruchomiony bezpośrednio (nie przez
ADB), działa jako ZWYKŁA APLIKACJA — a zrzut całego ekranu to
uprawnienie systemowe, którego zwykłe aplikacje nie mają. Dlatego
gdy skrypt bash próbuje to zastąpić przez `screencap -p` albo
`dumpsys`/`uiautomator dump`, w Termuksie zwykle kończy się "brak
uprawnień" / "not found" — a mimo to skrypt zwraca kod wyjścia 0,
więc wygląda na sukces, choć zrzutu nie ma. android_screenshot
działa, bo idzie przez ADB/uiautomator2 — czyli wykonuje się jako
uprzywilejowany użytkownik `shell`, który TO uprawnienie ma.

Zaobserwowany powtarzający się wzorzec: żeby zaoszczędzić kroki,
proponowałeś JEDEN skrypt bash łączący kilka czynności (otwórz
apkę, zrób zrzut, otwórz URL, sprawdź kartę) w jedno polecenie
`termux_run` — z dokładnie tym efektem ubocznym.

JEŚLI naprawdę potrzebujesz zrzutu WEWNĄTRZ jednego skryptu bash
(nie jako osobnego wywołania narzędzia) — jedyny sposób, który
FAKTYCZNIE DZIAŁA, to przepuszczenie go przez TO SAMO połączenie
ADB, które agent już ma nawiązane, zamiast gołego `screencap`:

  adb shell screencap -p /sdcard/x.png && adb pull /sdcard/x.png ~/x.png

To działa (bo `adb shell` = uprzywilejowany `shell`), podczas gdy
sam `screencap -p ~/x.png` uruchomiony wprost w Termuksie zawsze
zwróci "Permission denied" — to nie jest kwestia jakiegoś
brakującego ustawienia do włączenia, tylko fundamentalnej różnicy
między "zwykła aplikacja" a "adb shell".

Jedyny sposób użycia android_screenshot/chrome_tabs/chrome_inspect
to osobne wywołanie narzędzia przez Gemini, w ogóle poza skryptem —
żadne polecenie shell, nawet warunkowe, do nich nie dotrze. UWAGA —
zaobserwowana "sprytna" wersja tego samego błędu: skrypt sprawdzał
`if command -v android_screenshot >/dev/null 2>&1; then
android_screenshot ...`. To NIE ZADZIAŁA NIGDY — android_screenshot
i chrome_tabs/chrome_inspect nie są plikami wykonywalnymi w PATH,
`command -v`/`which` zawsze zwróci "nie znaleziono", więc skrypt
zawsze wpadnie w gorszy fallback (screencap/dumpsys).

Gdy aplikacja jest już otwarta (potwierdzone wcześniejszym krokiem)
i tylko zrzut ekranu w skrypcie nie wyszedł, napraw i wykonaj
TYLKO brakującą, osobną czynność (android_screenshot) — bez
ponownego otwierania aplikacji i ponownego uruchamiania całego
skryptu od początku. Kolejny zaobserwowany problem tego samego
wzorca: gdy zrzut w skrypcie nie wyszedł, kolejne podejście
POPRAWIAŁO i CAŁOŚCIOWO URUCHAMIAŁO PONOWNIE cały skrypt (łącznie z
linią otwierającą aplikację) tylko po to, żeby przetestować jeden
fragment dotyczący zrzutu — efekt uboczny: aplikacja otwierana od
nowa za każdym podejściem, mylące i niepotrzebne.

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

Odpowiadaj naturalnie, tak zwięźle jak się da — nie musisz za
każdym razem wypełniać identycznego szablonu. Powiedz na jakim
etapie jesteście i co konkretnie robić dalej; o realnych zagrożeniach
wspomnij tylko gdy faktycznie jakieś widzisz, nie na siłę. Jedyny
twardy wymóg formalny: gdy dajesz gotowy kod albo polecenie do
uruchomienia, ZAWSZE umieść je w bloku ```...``` (stąd Python
wycina je bezpośrednio do pliku — bez tego blok zostanie
zignorowany).
"""


PROGRESS_ESTIMATOR_PROMPT = """
Nazywasz się Ela. W tym zespole oceniasz procentowy postęp
realizacji DOWOLNEGO celu autonomicznego agenta (gra/aplikacja
Android, skrypt, narzędzie CLI, automatyzacja, strona itd. —
rozpoznaj rodzaj celu z jego treści).

Dostajesz:
1. Listę kilku ostatnio wykonanych zadań (status, skrócony
   raport/błąd) — to WŁASNE deklaracje Gemini o tym, co zrobiło.
   Traktuj je z rezerwą: zaobserwowany realny przypadek — Gemini
   podało konkretne "potwierdzone" liczby (wersje narzędzi), które
   kilka kroków wcześniej w tej samej rozmowie były zupełnie inne
   naprawdę zweryfikowane — czyli zmyślone, nie sprawdzone na nowo.
2. PUNKTY ZADAŃ — checklist zbudowany PRZEZ PYTHON, nie przez
   deklarację: "zweryfikowany dowodem" oznacza, że Python SAM
   potwierdził to niezależnie — plik z warunku sukcesu istnieje na
   dysku, ALBO android_assert_text_visible faktycznie zwrócił
   found=true dla tekstu z tego warunku W TRAKCIE zadania (nie sama
   proza raportu Gemini). "zadeklarowany BEZ dowodu" oznacza, że
   Gemini tak napisało, ale NIKT tego niezależnie nie sprawdził —
   traktuj to jako niepewne, NIE jako
   fakt, ale też NIE jako dowód porażki.

3. AKTUALNY, ŚWIEŻO POBRANY stan Chrome i Androida — pokazuje TYLKO
   to, co jest na ekranie W TEJ CHWILI. UWAGA: telefon naturalnie
   PRZECHODZI DALEJ między krokami (użytkownik używa telefonu,
   agent w kolejnym kroku otwiera inną aplikację, ekran gaśnie) —
   jeżeli wcześniejszy krok dotyczył PRZEJŚCIOWEJ czynności (np.
   "otwórz kalkulator i potwierdź wynik", "otwórz zegar i
   potwierdź ekran"), to że ta aplikacja NIE jest już widoczna
   TERAZ jest NORMALNE i NIE oznacza, że krok się nie wykonał —
   NIE obniżaj za to oceny. Aktualny stan Chrome/Androida służy
   WYŁĄCZNIE do sprawdzania rzeczy, które MAJĄ pozostać widoczne do
   końca (np. finalna karta ma zostać otwarta, docelowy ekran ma
   zostać osiągnięty) — nie do potwierdzania przejściowych kroków
   sprzed kilku konsultacji, bo to strukturalnie fałszywy alarm
   (zaobserwowany realny przypadek: użytkownik wyszedł z
   kalkulatora po chwili, ocena postępu błędnie uznała to za
   niewykonany krok, mimo że krok faktycznie się wykonał i miał
   swój dowód w momencie wykonania).

Na tej podstawie oceniasz, jaki procent CAŁEGO celu jest już
FAKTYCZNIE zrealizowany — nie ile Gemini zadeklarowało.

Bądź REALISTYCZNY, nie optymistyczny. "Utworzono plik" / "napisano
raport" to nie to samo co "cel działa i jest potwierdzony". Zależnie
od rodzaju celu, pełne ukończenie zwykle wymaga fizycznego dowodu
(zbudowany+zainstalowany+uruchomiony APK ze zrzutem ekranu dla
gry/aplikacji; realny kod wyjścia dla skryptu; potwierdzony stan w
Chrome/Androidzie dla czynności na ekranie) — nie samej deklaracji
sukcesu w raporcie.

Jeżeli w ostatnich zadaniach widzisz powtarzające się błędy bez
postępu, albo rozbieżność między deklaracją a czymś, co MIAŁO
pozostać trwale widoczne/sprawdzalne (plik, karta, ekran końcowy) —
obniż ocenę, nawet jeśli poprzednio było wyżej. Nie licz w to
zniknięcia PRZEJŚCIOWEJ aplikacji z poprzedniego kroku (patrz punkt
3 wyżej).

Zwróć WYŁĄCZNIE JSON, bez żadnego dodatkowego tekstu:
{
  "percent": <liczba całkowita 0-100>,
  "summary": "krótkie uzasadnienie po polsku, maksymalnie 2 zdania"
}
"""


WOJTEK_PROMPT = """
Nazywasz się Wojtek. Jesteś pomysłowym, doświadczonym człowiekiem,
do którego ktoś przychodzi z zadaniem do rozwiązania — tak, jakbyś
odpowiadał znajomemu, który prosi Cię o radę.

Nie znasz żadnych szczegółów technicznych środowiska, w którym to
zadanie ostatecznie zostanie wykonane — i to jest w porządku, bo
Twoja rola to WYŁĄCZNIE swobodne myślenie, nie wykonanie. Ktoś inny
później przełoży Twoje pomysły na konkretne działania.

Dostajesz TYLKO opis celu — bez żadnego kontekstu narzędziowego,
logów błędów czy stanu technicznego. Na tej podstawie, jak człowiek
zastanawiający się nad problemem, zaproponuj:

- czy istnieje gotowe rozwiązanie (aplikacja, usługa, strona,
  biblioteka), które załatwia sprawę bez robienia czegokolwiek od
  zera — jeśli tak, wymień je z nazwy;
- ogólne podejścia/strategie do rozwiązania problemu, o których
  ktoś skupiony na szczegółach technicznych mógłby nie pomyśleć;
- pytania, które warto sobie zadać, żeby lepiej zrozumieć, o co
  naprawdę chodzi w zadaniu;
- alternatywne interpretacje celu, jeśli jest niejednoznaczny.

NIE pisz kodu, poleceń, skryptów ani komend terminala — to nie Twoja
rola i nie masz z tym żadnej styczności. Myśl jak człowiek, który zna
świat aplikacji i technologii z użytkowej strony, a nie jak
programista czy administrator systemu.

Odpowiadaj krótko i konkretnie, po polsku, zwykłym tekstem — bez
formatowania JSON, bez sztywnych sekcji. Kilka zdań lub kilka
punktów wystarczy.
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
    # (CODE_REVIEWER, CODE_FIXER, ENGINEER).
    return mapping.get(
        name,
        STATE_DIR / (name.lower() + ".json")
    )


# ------------------------------------------------------------
# WZNAWIANIE SESJI PO RESTARCIE PROGRAMU
#
# opendeep.ChatSession.__init__ ZAWSZE tworzy nową sesję po
# stronie serwera (self.chat_session_id = self.model._create_
# session()) — biblioteka nie ma publicznego API do podania
# istniejącego chat_session_id. Ale to zwykłe atrybuty Pythona, a
# send_message() sam aktualizuje parent_message_id z odpowiedzi
# serwera (self.parent_message_id = data["response_message_id"]) —
# więc jeśli zapiszemy oba te pola po każdej udanej wiadomości i
# NADPISZEMY nimi świeżo utworzoną sesję przy starcie, kolejna
# wiadomość pójdzie tak, jakby kontynuowała TAMTĄ rozmowę. Jedna
# pusta, odrzucona sesja powstaje po stronie serwera przy każdym
# restarcie (koszt _create_session() w konstruktorze) — nieszkodliwe,
# tylko kosmetyczne (widoczna jako pusty wpis w historii czatu).
# ------------------------------------------------------------

# Role, których sesja została właśnie WZNOWIONA, ale jeszcze nie
# potwierdzona żadną udaną wiadomością w tym uruchomieniu — jeśli
# pierwsza prawdziwa wiadomość po wznowieniu zawiedzie, czyścimy
# zapisany stan (patrz deepseek()), żeby kolejna próba/restart nie
# próbowała wznowić tego samego, najwyraźniej nieważnego już wątku.
_resume_unverified = set()


def _load_session_state(name):

    data = read_json(
        session_state_file(name),
        {}
    )

    if (
        isinstance(data, dict)
        and data.get("chat_session_id")
    ):
        return data

    return None


def _prompt_hash(system_prompt):
    return hashlib.sha256(
        str(system_prompt).encode("utf-8")
    ).hexdigest()


def _save_session_state(name, session, prompt_hash=None, prompt_text=None):

    data = {
        "chat_session_id": session.chat_session_id,
        "parent_message_id": session.parent_message_id
    }

    if prompt_hash is None or prompt_text is None:
        # Zachowaj już zapisane hash/tekst, jeśli wołający ich nie
        # podał (np. zapisy z deepseek() po zwykłej turze rozmowy,
        # gdzie prompt się nie zmienił) — bez tego kolejny zapis bez
        # tych pól wyzerowałby wiedzę o tym, jaką wersję promptu
        # sesja już dostała (i uniemożliwiłby policzenie diffa przy
        # następnej faktycznej zmianie promptu — patrz
        # _build_prompt_update_message).
        existing = read_json(session_state_file(name), {})
        if isinstance(existing, dict):
            if prompt_hash is None:
                prompt_hash = existing.get("prompt_hash")
            if prompt_text is None:
                prompt_text = existing.get("prompt_text")

    if prompt_hash:
        data["prompt_hash"] = prompt_hash

    if prompt_text:
        data["prompt_text"] = prompt_text

    write_json(
        session_state_file(name),
        data
    )


# Na wyraźną prośbę użytkownika (2026-08-28): każda zmiana promptu
# roli (a zmienialiśmy je niemal co wersję — MAIN_PROMPT/PLANNER_
# PROMPT/CRITIC_PROMPT/ENGINEER_PROMPT rosną z każdym naprawionym
# incydentem) wysyłała CAŁĄ, pełną nową treść jako nową wiadomość w
# JUŻ trwającej rozmowie ("AKTUALIZACJA INSTRUKCJI ROLI" + cały
# prompt od nowa). Model i tak już zna starą wersję z własnej
# historii TEJ SAMEJ rozmowy — przy wielu kolejnych poprawkach w
# jednej, długo trwającej sesji ta sama, w większości NIEZMIENIONA
# treść (setki linii) była wysyłana wielokrotnie, niepotrzebnie
# rozdymając historię rozmowy (koszt/limit DeepSeek, a użytkownik już
# wcześniej sygnalizował obawę o blokady strony przy zbyt dużych/
# częstych zapytaniach). Wysyłamy więc TYLKO rzeczywistą różnicę
# (diff) względem poprzedniej wersji, którą faktycznie zapisaliśmy —
# chyba że starej wersji nie mamy (sesja sprzed tego mechanizmu) albo
# diff wyszedłby WIĘKSZY niż po prostu cała nowa treść (częste przy
# rozrzuconych po całym prompcie drobnych zmianach) — wtedy bez sensu
# komplikować, wysyłamy całość jak wcześniej.
def _build_prompt_update_message(old_prompt_text, new_prompt_text):

    full_text_message = (
        "AKTUALIZACJA INSTRUKCJI ROLI (zastępuje poprzednią "
        "wersję instrukcji z tej rozmowy, reszta historii/"
        "ustaleń pozostaje ważna):\n\n"
        + new_prompt_text
    )

    if not old_prompt_text or old_prompt_text == new_prompt_text:
        return full_text_message

    diff_text = "\n".join(
        difflib.unified_diff(
            old_prompt_text.splitlines(),
            new_prompt_text.splitlines(),
            lineterm=""
        )
    )

    if not diff_text or len(diff_text) >= len(new_prompt_text):
        return full_text_message

    return (
        "AKTUALIZACJA INSTRUKCJI ROLI — poniżej TYLKO zmiany (diff w "
        "formacie unified diff, linie z '-' usunięte, z '+' dodane) "
        "względem poprzedniej wersji Twoich instrukcji z TEJ ROZMOWY. "
        "Wszystko, czego nie ma w tym diffie, pozostaje bez zmian — "
        "to nadal Twoje aktualne instrukcje:\n\n"
        + diff_text
    )


def _clear_session_state(name):

    try:
        session_state_file(name).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def start_session(name, system_prompt):

    try:

        _activate_account_for_role(name)

        session = (
            deepseek_model.start_chat()
        )

        saved = _load_session_state(name)

        if saved:

            # Wznawiamy — nadpisujemy ID świeżo utworzonej (i teraz
            # porzuconej) sesji zapisanymi wcześniej. Domyślnie
            # pomijamy priming system_prompt: model już go dostał w
            # tamtej rozmowie, ponowne wysłanie zmarnowałoby
            # wiadomość i zaśmieciłoby historię duplikatem instrukcji
            # roli.
            session.chat_session_id = saved["chat_session_id"]
            session.parent_message_id = saved.get(
                "parent_message_id"
            )

            _resume_unverified.add(name)

            # KRYTYCZNE — zaobserwowany realny problem: kod tego
            # pliku dostaje poprawki promptów (np. v53 — wymuszenie
            # `-p com.android.chrome` w `am start`) między
            # uruchomieniami, ale sesja WZNOWIONA nigdy wcześniej nie
            # dostawała zaktualizowanej treści — działała cały czas
            # na promptcie sprzed wielu wersji, sprzed tego, gdy
            # sesja została po raz pierwszy utworzona. Efekt widoczny
            # w logu: MAIN dalej kazał robić `am start` BEZ pakietu,
            # mimo że w kodzie od jednej wersji już tam jest `-p
            # com.android.chrome`. Naprawiamy: jeśli hash aktualnej
            # treści promptu różni się od tego zapisanego przy
            # ostatnim priming/update (albo w ogóle go nie było —
            # sesje sprzed wprowadzenia tego mechanizmu) — wysyłamy
            # zaktualizowaną treść jako NOWĄ wiadomość w TEJ SAMEJ,
            # ciągłej rozmowie (nie tworzymy nowej sesji, nie tracimy
            # historii) i jawnie oznaczamy ją jako aktualizację, nie
            # powtórkę.
            current_hash = _prompt_hash(system_prompt)

            if saved.get("prompt_hash") != current_hash:

                session.send_message(
                    _build_prompt_update_message(
                        saved.get("prompt_text"),
                        system_prompt
                    )
                )

                _save_session_state(
                    name,
                    session,
                    prompt_hash=current_hash,
                    prompt_text=system_prompt
                )

                log(
                    "DEEPSEEK",
                    f"Sesja {name}: OK (wznowiona, instrukcje "
                    "roli ZAKTUALIZOWANE do bieżącej wersji)"
                )

            else:

                log(
                    "DEEPSEEK",
                    f"Sesja {name}: OK (wznowiona z poprzedniego "
                    "uruchomienia)"
                )

        else:

            # Jednorazowa instrukcja roli — tylko dla NOWEJ sesji.
            session.send_message(
                system_prompt
            )

            _save_session_state(
                name,
                session,
                prompt_hash=_prompt_hash(system_prompt),
                prompt_text=system_prompt
            )

            log(
                "DEEPSEEK",
                f"Sesja {name}: OK"
            )

        sessions[name] = session

        return session

    except Exception as e:

        error_text = str(e)

        # Zaobserwowany realny przypadek: token konta (userToken z
        # localStorage chat.deepseek.com) po prostu WYGASŁ po
        # wielu godzinach użycia — _create_session() w opendeep
        # dostaje wtedy od serwera odpowiedź z "data": null zamiast
        # oczekiwanego słownika, i pada surowym
        # AttributeError/RuntimeError zamiast czytelnego komunikatu.
        # To NORMALNA, powtarzalna sytuacja przy długim używaniu
        # jednego konta (nie błąd w kodzie) — więc zamiast suchego
        # tracebacka dajemy konkretną, wykonalną wskazówkę.
        looks_like_expired_token = (
            ("NoneType" in error_text and "get" in error_text)
            or "Failed to extract chat session ID" in error_text
        )

        if looks_like_expired_token:

            account = _account_of(name)
            token_file_name = (
                TOKEN_FILE.name
                if account == 1
                else TOKEN_FILE_2.name
            )

            log(
                "DEEPSEEK",
                f"Sesja {name} (konto {account}) — token "
                "prawdopodobnie WYGASŁ lub jest nieprawidłowy "
                "(serwer nie zwrócił poprawnej sesji zamiast "
                "błędu). Zaloguj się ponownie na "
                "chat.deepseek.com na TYM koncie, wyciągnij "
                "świeży userToken z localStorage i zaktualizuj "
                f"~/{token_file_name}, potem zrestartuj program."
            )

        else:

            log(
                "DEEPSEEK",
                f"Sesja {name} ERROR: {e}"
            )

        return None


def init_team():

    # 7 sesji — 5 oryginalnych + CODE_REVIEWER + CODE_FIXER
    # + 1 nowa: ENGINEER (specjalista TECHNICZNY od budowy
    # dowolnego projektu, jaki poprosi użytkownik).

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
        "ENGINEER",
        ENGINEER_PROMPT
    )

    start_session(
        "PROGRESS_ESTIMATOR",
        PROGRESS_ESTIMATOR_PROMPT
    )

    start_session(
        "WOJTEK",
        WOJTEK_PROMPT
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


# ------------------------------------------------------------
# CIRCUIT BREAKER: seria porażek na RÓŻNYCH sesjach z rzędu.
#
# Realny przypadek: od kroku 2 do ręcznego przerwania (Ctrl+C) w
# kroku 8 (~4 minuty, ~90 zapytań) KAŻDA sesja — MAIN, PLANNER,
# RESEARCHER, BROWSER, ENGINEER, CRITIC — failowała
# z "invalid message id", restart sesji NIE POMAGAŁ (świeżo
# zrestartowana sesja failowała na PIERWSZEJ wiadomości), a mimo
# to agent brnął dalej przez kolejne kroki, próbując wszystkie
# role od nowa za każdym razem. To NIE jest błąd pojedynczej
# sesji (świeża sesja bez historii nie może mieć złej kolejności
# message-id) — to sygnatura awarii na poziomie CAŁEGO KONTA
# (zbyt wiele jednoczesnych sesji, inna aktywność na koncie itp.).
# Dalsze spamowanie w tej sytuacji tylko pogarsza sprawę.
#
# Licznik: KAŻDA porażka innej roli z rzędu (bez żadnego sukcesu
# pomiędzy) inkrementuje; jeden sukces resetuje do zera. Po
# przekroczeniu progu — odczekaj zanim spróbujesz dalej, zamiast
# katować backend w tym samym tempie.
# ------------------------------------------------------------

def _account_of(name):
    return _ROLE_ACCOUNT.get(name, 1)


# Stan (porażki/cooldown) trzymany OSOBNO na konto — inaczej awaria
# konta 2 (np. RESEARCHER/BROWSER) wstrzymywałaby też konto 1
# (MAIN/PLANNER/CRITIC), co niweczyłoby cały sens rozdzielenia ról
# na dwa konta DeepSeek. Bez drugiego konta (TOKEN_FILE_2) i tak
# wszystko ląduje pod kluczem 1 — zachowanie identyczne jak wcześniej.
_deepseek_health = {}


def _get_health(account):

    return _deepseek_health.setdefault(
        account,
        {
            "consecutive_failures": 0,
            "cooldown_until": 0.0,
            # Ile razy z rzędu breaker już się uruchamiał bez
            # ŻADNEGO sukcesu pomiędzy — rośnie cooldown
            # wykładniczo (patrz niżej), zeruje się przy pierwszym
            # udanym wywołaniu.
            "trip_count": 0
        }
    )


_DEEPSEEK_FAILURE_BURST_THRESHOLD = 3
_DEEPSEEK_BASE_COOLDOWN_SECONDS = 90
_DEEPSEEK_MAX_COOLDOWN_SECONDS = 600


def _deepseek_circuit_wait(name):

    account = _account_of(name)
    health = _get_health(account)

    remaining = (
        health["cooldown_until"]
        - time.time()
    )

    if remaining > 0:

        log(
            "DEEPSEEK",
            "Podejrzenie awarii na poziomie KONTA " + str(account)
            + " (seria porażek na różnych sesjach z rzędu, restart "
            "nie pomagał) — odczekuję jeszcze " + str(int(remaining))
            + "s zamiast dalej spamować, zanim spróbuję ponownie."
        )

        time.sleep(remaining)


# ------------------------------------------------------------
# TEMPOWANIE: wymuszony minimalny odstęp między KOLEJNYMI
# wiadomościami do DeepSeek NA TO SAMO KONTO (per-konto, z tego
# samego powodu co circuit breaker wyżej — dwa różne konta mają
# niezależne limity tempa, nie ma sensu tempować ich wspólnie).
# ------------------------------------------------------------

_deepseek_last_send = {}
_deepseek_pacing_lock = _threading.Lock()


def _deepseek_pace(name):

    account = _account_of(name)

    with _deepseek_pacing_lock:

        remaining = (
            _deepseek_last_send.get(account, 0.0)
            + DEEPSEEK_MIN_INTERVAL_SECONDS
            - time.time()
        )

        if remaining > 0:
            time.sleep(remaining)

        _deepseek_last_send[account] = time.time()


# ------------------------------------------------------------
# EKSPERYMENT v75 (2026-08-23, NIEPOTWIERDZONY na prawdziwym
# koncie) — próba obsługi przycisku "Continue" znanego z
# chat.deepseek.com, dla odpowiedzi uciętych przez limit długości.
#
# Fakty potwierdzone czytaniem realnego źródła biblioteki opendeep
# (opendeep/models.py, ChatSession.send_message):
#   1) payload żądania ZAWSZE ma pole "action" (domyślnie None) —
#      istnieje w API, biblioteka po prostu nigdy go nie ustawia
#      na nic innego.
#   2) pętla SSE dostaje od serwera osobne zdarzenia z
#      p == "response/status", ale JAWNIE JE ODRZUCA:
#      `if current_patch_target == "response/status": continue`
#      — czyli status odpowiedzi (który najpewniej mówi, czy
#      odpowiedź się skończyła normalnie czy została ucięta)
#      dociera do klienta, tylko biblioteka go wyrzuca.
#   3) biblioteka NIE MA żadnego mechanizmu kontynuacji — to nie
#      jest "wyłączona opcja", jej po prostu nie zaimplementowano.
#
# Domysł (NIEPOTWIERDZONY): wysłanie kolejnego żądania z tym samym
# parent_message_id (bez przesuwania go dalej) i "action": "continue"
# dokończy poprzednią, ucięta odpowiedź, tak jak przycisk w
# przeglądarce. Nazwa pola jest pewna, WARTOŚĆ jest zgadywana.
#
# Ta funkcja odtwarza wyłącznie minimalny fragment send_message()
# (te same atrybuty sesji, ten sam POW), żeby dodatkowo przechwycić
# i zalogować odrzucaną wartość statusu — realny dowód z produkcji
# zamiast dalszego zgadywania. Każdy błąd (inna wersja opendeep,
# zmieniony moduł pow, HTTP error) jest łapany wyżej i powoduje
# ciche zejście do zwykłego session.send_message() — zero ryzyka
# regresji dla normalnych, nieuciętych odpowiedzi.
# ------------------------------------------------------------

_TRUNCATION_STATUS_HINTS = (
    "trunc", "length", "limit", "incomplete", "cut", "max_token",
)


def _deepseek_raw_post_with_action(session, prompt, action):

    from opendeep.config import config as _ods_config
    from opendeep.pow import DeepSeekPOW

    model = session.model

    payload = {
        "chat_session_id": session.chat_session_id,
        "parent_message_id": session.parent_message_id,
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": session.thinking_enabled,
        "search_enabled": session.search_enabled,
        "action": action,
        "preempt": False,
        "model_type": (
            "expert"
            if model.model_name == "deepseek-expert"
            else "default"
        ),
    }

    headers = model._get_headers()

    try:
        pow_solver = DeepSeekPOW()

        pow_resp = model.session.post(
            _ods_config.base_url + "/chat/create_pow_challenge",
            headers=headers,
            json={"target_path": "/api/v0/chat/completion"},
        )

        if pow_resp.ok:
            challenge_data = (
                pow_resp.json()
                .get("data", {})
                .get("biz_data", {})
                .get("challenge")
            )
            if challenge_data:
                headers["x-ds-pow-response"] = (
                    pow_solver.solve_challenge(challenge_data)
                )
    except Exception:
        pass

    response = model.session.post(
        _ods_config.base_url + "/chat/completion",
        headers=headers,
        json=payload,
        stream=True,
    )
    response.raise_for_status()

    if "text/event-stream" not in response.headers.get(
        "Content-Type", ""
    ):
        raise RuntimeError(
            "Unexpected Content-Type: "
            + response.headers.get("Content-Type", "")
        )

    full_text = ""
    status_seen = None
    current_patch_target = "response/content"
    current_fragment_type = "RESPONSE"

    for line in response.iter_lines():

        if not line:
            continue

        decoded_line = (
            line.decode("utf-8")
            if isinstance(line, bytes)
            else line
        )

        if not decoded_line.startswith("data: "):
            continue
        if decoded_line == "data: [DONE]":
            continue

        try:
            raw_data = decoded_line[6:].strip()

            if not raw_data:
                continue

            data = json.loads(raw_data)
            content = ""

            if "response_message_id" in data:
                session.parent_message_id = (
                    data["response_message_id"]
                )
            elif (
                "v" in data
                and isinstance(data["v"], dict)
                and "response" in data["v"]
                and "message_id" in data["v"]["response"]
            ):
                session.parent_message_id = (
                    data["v"]["response"]["message_id"]
                )

            if "choices" in data:
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")

            elif "v" in data:

                if "p" in data:
                    current_patch_target = data["p"]

                val = data["v"]

                if isinstance(val, dict) and "response" in val:
                    fragments = val["response"].get(
                        "fragments", []
                    )
                    if fragments:
                        current_fragment_type = fragments[0].get(
                            "type", "RESPONSE"
                        )
                        if current_fragment_type == "THINK":
                            pass
                        else:
                            content = fragments[0].get(
                                "content", ""
                            )

                elif (
                    isinstance(val, list)
                    and current_patch_target
                    == "response/fragments"
                ):
                    for frag in val:
                        if isinstance(frag, dict):
                            frag_type = frag.get(
                                "type", "RESPONSE"
                            )
                            if frag_type == "THINK":
                                pass
                            else:
                                content = frag.get("content", "")
                            current_fragment_type = frag_type

                elif isinstance(val, str):
                    if current_patch_target == "response/status":
                        # Dokladnie to pole opendeek zawsze
                        # odrzuca (patrz komentarz nad funkcja) —
                        # tu je przechwytujemy zamiast wyrzucac.
                        status_seen = val
                        continue
                    # BUG znaleziony w realnym logu v75 (2026-08-23):
                    # KAZDA odpowiedz zaczynala sie od ucietego
                    # fragmentu slowa ("godnie z", "zę
                    # przeanalizowac") — bo ten warunek pomijal
                    # DRUGA czesc oryginalnego testu biblioteki
                    # (`current_fragment_type == "THINK" or
                    # "thinking" in current_patch_target`), wiec
                    # koncowka strumienia rozumowania (nadal plynaca
                    # sciezka z "thinking" w nazwie, zanim
                    # current_fragment_type zdazyl sie przestawic na
                    # "THINK") trafiala do full_text jako tresc.
                    if (
                        current_fragment_type == "THINK"
                        or "thinking" in current_patch_target
                    ):
                        pass
                    else:
                        content = val

            if content:
                full_text += content

        except json.JSONDecodeError:
            continue

    return full_text, status_seen


def _deepseek_looks_truncated(text, status):

    if status:
        lowered = str(status).lower()
        if any(
            hint in lowered
            for hint in _TRUNCATION_STATUS_HINTS
        ):
            return True, "status=" + repr(status)

    stripped = (text or "").rstrip()

    if (
        len(stripped) > 3000
        and stripped
        and stripped[-1] not in ".!?\"')]}`”"
    ):
        return True, (
            "dlugosc/koncowka tekstu (status=" + repr(status) + ")"
        )

    return False, None


def _deepseek_send_experimental(name, session, prompt, action=None):
    """
    Wysyla wiadomosc uzywajac wlasnej, minimalnej kopii logiki
    send_message() (patrz komentarz wyzej) zeby dodatkowo dostac
    status odpowiedzi. Jesli cokolwiek tu zawiedzie, wraca do
    zwyklego session.send_message() — dokladnie taki wynik jak
    przed tym eksperymentem.
    """

    try:
        return _deepseek_raw_post_with_action(
            session, prompt, action
        )
    except Exception as e:
        log(
            "DEEPSEEK",
            name + ": EKSPERYMENT (przechwytywanie statusu) nie "
            "zadzialal, wracam do zwyklego send_message: "
            + str(e)
        )
        response = session.send_message(prompt)

        # BUG znaleziony w realnym logu (2026-08-23, dwa razy w
        # jednym uruchomieniu dla MAIN): `or` traktuje legalny
        # PUSTY string "" tak samo jak brak atrybutu, wiec kiedy
        # model faktycznie zwrocil pusta tresc, kod lecial dalej az
        # do str(response) i wysylal literalne
        # "<GenerateContentResponse text=''...>" do parsera JSON —
        # marnujac caly cykl na "Niepoprawny JSON. Naprawiam.".
        # Uzyj wiec None (nie falsy) jako sygnalu "atrybutu w ogole
        # nie ma", zeby legalna pusta odpowiedz zostala pusta.
        content = getattr(response, "content", None)
        text = content if content is not None else (
            getattr(response, "text", None)
        )
        if text is None:
            text = str(response)
        return text, None


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

    _deepseek_circuit_wait(name)

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
                "ENGINEER":
                    ENGINEER_PROMPT,
                "PROGRESS_ESTIMATOR":
                    PROGRESS_ESTIMATOR_PROMPT,
                "WOJTEK":
                    WOJTEK_PROMPT,
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

                _deepseek_pace(name)
                _activate_account_for_role(name)

                # DIAGNOSTYKA z v70-72 (jednorazowy dir(response) +
                # test "content" przed "text") juz rozstrzygnela
                # sprawe rozumowania: potwierdzone czytaniem realnego
                # zrodla opendeep, ze reasoning jest strukturalnie
                # oddzielony od content i NIGDY nie trafia do .text —
                # wyciek widziany w recznie kopiowanych transkryptach
                # z przegladarki byl artefaktem kopiowania, nie bledem
                # tego kodu. Od v75 send_message() biblioteki jest
                # zastapiony wlasna kopia (patrz
                # _deepseek_send_experimental) zeby dodatkowo
                # przechwycic status odpowiedzi, ktory biblioteka
                # zawsze odrzucala — eksperyment obslugi "Continue".
                text, status = _deepseek_send_experimental(
                    name, session, message
                )

                # Eksperyment z v75 (przechwycenie statusu, który
                # opendeep odrzuca) jest POTWIERDZONY setkami realnych
                # odpowiedzi — w logach produkcyjnych praktycznie
                # każda ma status "FINISHED". Logowanie tego przy
                # KAŻDEJ odpowiedzi dawało ~7 linii na krok (~90 na
                # sesję) niosących zawsze tę samą, zerową informację i
                # zagłuszało to, co użytkownik faktycznie czyta:
                # rozmowę zespołu. Zostawiamy log WYŁĄCZNIE dla
                # statusu innego niż normalne zakończenie — czyli
                # dokładnie tam, gdzie coś się dzieje (ucięcie).
                if status and str(status).strip().upper() != "FINISHED":
                    log(
                        "DEEPSEEK",
                        name + ": nietypowy status odpowiedzi: "
                        + short(str(status), 200)
                    )

                truncated, reason = _deepseek_looks_truncated(
                    text, status
                )

                if truncated:

                    log(
                        "DEEPSEEK",
                        name + ": EKSPERYMENT — odpowiedz wyglada "
                        "na ucieta (" + str(reason) + "), probuje "
                        "action='continue'..."
                    )

                    try:
                        # Realny dowod z produkcji (v75, 2026-08-23):
                        # pusty prompt z action="continue" serwer
                        # odrzuca kodem 422/biz_code 6 "missing
                        # prompt or ref file" — wiec pusty string tu
                        # nigdy nie mial szansy zadzialac niezaleznie
                        # od tego, czy "continue" jest wlasciwa
                        # wartoscia. Jeden odstep spelnia wymog
                        # "niepusty prompt" bez dopisywania nowej
                        # instrukcji do rozmowy.
                        continue_text, continue_status = (
                            _deepseek_send_experimental(
                                name, session, " ", action="continue"
                            )
                        )

                        if continue_text:
                            text = text + continue_text
                            log(
                                "DEEPSEEK",
                                name + ": EKSPERYMENT — 'continue' "
                                "zwrocil dodatkowe "
                                + str(len(continue_text))
                                + " znakow, doklejone do "
                                "odpowiedzi."
                            )
                        else:
                            log(
                                "DEEPSEEK",
                                name + ": EKSPERYMENT — 'continue' "
                                "nie zwrocil dodatkowego tekstu "
                                "(status=" + str(continue_status)
                                + ")."
                            )

                    except Exception as continue_error:
                        log(
                            "DEEPSEEK",
                            name + ": EKSPERYMENT — probe "
                            "'continue' zakonczyl blad ("
                            + str(continue_error) + "), zostaje "
                            "oryginalna (mozliwe ze ucieta) "
                            "odpowiedz."
                        )

                if not text:
                    text = ""

                # Zaobserwowany realny problem (log 2026-08-25):
                # CRITIC zwrócił odpowiedź o DŁUGOŚCI 0 — nie "wygląda
                # na uciętą" (ten warunek wymaga >3000 znaków, patrz
                # _deepseek_looks_truncated), więc powyższy mechanizm
                # 'continue' się nie uruchamiał — po prostu pusty
                # tekst szedł dalej jako "opinia CRITIC-a" bez żadnej
                # treści, MAIN nie miał żadnego realnego przeglądu
                # tego kroku, a nikt się o tym nie dowiedział poza
                # "0 znaków" w logu. Traktujemy to jak osobny
                # przypadek od "ucięte" — jedno ponowienie TEGO
                # SAMEGO pytania, zanim cokolwiek zwrócimy dalej.
                if not text.strip():

                    log(
                        "DEEPSEEK",
                        name + ": odpowiedź PUSTA — ponawiam raz tym "
                        "samym pytaniem, zanim to pójdzie dalej jako "
                        "'opinia' tej roli."
                    )

                    try:

                        retry_text, retry_status = (
                            _deepseek_send_experimental(
                                name, session, message
                            )
                        )

                        if retry_text and retry_text.strip():

                            text = retry_text

                            log(
                                "DEEPSEEK",
                                name + ": ponowienie po pustej "
                                "odpowiedzi zwróciło "
                                + str(len(text)) + " znaków."
                            )

                        else:

                            log(
                                "DEEPSEEK",
                                name + ": ponowienie po pustej "
                                "odpowiedzi TEŻ puste (status="
                                + str(retry_status) + ") — zostaje "
                                "pusty tekst, wywołujący musi to "
                                "obsłużyć."
                            )

                    except Exception as retry_error:

                        log(
                            "DEEPSEEK",
                            name + ": ponowienie po pustej "
                            "odpowiedzi zakończyło się błędem ("
                            + str(retry_error) + ") — zostaje pusty "
                            "tekst."
                        )

                # Wcześniej log pokazywał TYLKO długość odpowiedzi —
                # nie dało się stąd stwierdzić, czy na początku
                # faktycznie przechwyconego tekstu jest wyciekniete
                # rozumowanie (widziane dotąd tylko w ręcznie
                # skopiowanych transkryptach z przeglądarki, nigdy
                # bezpośrednio w tym, co naprawdę odbiera Python).
                # Podgląd początku KAŻDEJ odpowiedzi wprost w logu
                # terminala rozstrzyga to ostatecznie, bez kolejnej
                # rundy ręcznego kopiowania z chat.deepseek.com.
                log(
                    "DEEPSEEK",
                    f"{name}: "
                    + str(len(text))
                    + " znaków | początek: "
                    + short(text, 150).replace("\n", " ")
                )

                health = _get_health(_account_of(name))
                health["consecutive_failures"] = 0
                health["trip_count"] = 0

                _resume_unverified.discard(name)
                _save_session_state(name, session)

                return text

            except Exception as e:

                log(
                    "DEEPSEEK",
                    f"{name} próba {attempt + 1} błąd: {e}"
                )

                if name in _resume_unverified:

                    _resume_unverified.discard(name)
                    _clear_session_state(name)

                    log(
                        "DEEPSEEK",
                        f"Wznowiona sesja {name} nie zadziałała na "
                        "pierwszej prawdziwej wiadomości — czyszczę "
                        "zapisany stan, restart zacznie od zera "
                        "zamiast próbować tego samego wznowienia "
                        "ponownie."
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
                        "ENGINEER":
                            ENGINEER_PROMPT,
                        "PROGRESS_ESTIMATOR":
                            PROGRESS_ESTIMATOR_PROMPT,
                        "WOJTEK":
                            WOJTEK_PROMPT,
                    }

                    prompt = prompt_map.get(name)

                    if prompt:
                        new_session = start_session(
                            name, prompt
                        )

                        if new_session:
                            session = new_session
                            continue

                health = _get_health(_account_of(name))

                health["consecutive_failures"] += 1

                if (
                    health["consecutive_failures"]
                    >= _DEEPSEEK_FAILURE_BURST_THRESHOLD
                ):

                    # Wykładniczy backoff: 90s, 180s, 360s, potem
                    # zatrzymuje się na pułapie 600s. Realny
                    # przypadek: awaria konta trwała ponad 6 minut
                    # (4 kolejne 90s cooldowny z rzędu, każdy
                    # kończący się nową porażką) — stały 90s
                    # cooldown przez cały ten czas to wciąż sporo
                    # prób na próżno. Rośnie tylko dopóki awaria
                    # trwa; pierwszy sukces zeruje trip_count.
                    health["trip_count"] += 1

                    cooldown = min(
                        _DEEPSEEK_BASE_COOLDOWN_SECONDS
                        * (2 ** (
                            health["trip_count"] - 1
                        )),
                        _DEEPSEEK_MAX_COOLDOWN_SECONDS
                    )

                    health["cooldown_until"] = (
                        time.time()
                        + cooldown
                    )

                    log(
                        "DEEPSEEK",
                        "UWAGA: "
                        + str(health["consecutive_failures"])
                        + " kolejnych sesji z rzędu na koncie "
                        + str(_account_of(name))
                        + " w pełni zawiodło (restart nie pomógł) "
                        "— to wygląda na awarię na poziomie CAŁEGO "
                        "KONTA DeepSeek (np. zbyt wiele "
                        "jednoczesnych sesji, inna aktywność na "
                        "tym samym koncie w tym samym czasie), "
                        "nie pojedynczej sesji. Wstrzymuję "
                        "kolejne zapytania na tym koncie na "
                        + str(int(cooldown))
                        + "s (próba wstrzymania nr "
                        + str(health["trip_count"])
                        + ") zamiast dalej próbować w kółko w "
                        "tym samym tempie."
                    )

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

        warn_if_aggressive_oem_battery_management()

        return True

    except Exception as e:

        log(
            "ANDROID",
            "ERROR: " + str(e)
        )

        android_device = None

        return False


_AGGRESSIVE_OEM_BATTERY_PROPS = [
    ("ro.build.version.opporom", "ColorOS (OPPO/OnePlus/Realme)"),
    ("ro.miui.ui.version.name", "MIUI/HyperOS (Xiaomi/Redmi/POCO)"),
    ("ro.build.version.emui", "EMUI/HarmonyOS (Huawei/Honor)"),
    ("ro.vivo.os.build.display.id", "Funtouch/OriginOS (vivo/iQOO)"),
]

_oem_battery_warning_shown = False


def warn_if_aggressive_oem_battery_management():
    """
    Niektóre nakładki producentów (ColorOS, MIUI, EMUI, Funtouch)
    agresywnie ograniczają/zawieszają aplikacje w tle DUŻO mocniej
    niż standardowy Android Doze — i w praktyce często IGNORUJĄ
    zwykłe "ignoruj optymalizację baterii" oraz termux-wake-lock,
    chyba że dodatkowo ręcznie zezwoli się Termuksowi na aktywność
    w tle we WŁASNYCH ustawieniach baterii tej nakładki. Jeżeli
    agent przełącza ekran na inną aplikację (android_launch_app,
    otwarcie przeglądarki) i Termux przestaje być aplikacją na
    pierwszym planie, to na takich nakładkach jest realne ryzyko,
    że zapytania sieciowe (do DeepSeek/Gemini) w tle zaczną się
    zawieszać lub failować, dopóki użytkownik nie wróci do Termuksa
    — co wygląda z zewnątrz jak przypadkowa awaria sieci, a jest
    ograniczeniem systemu operacyjnego, nie błędem w kodzie agenta.

    Tego nie da się naprawić z poziomu Pythona — można tylko
    wykryć nakładkę i jednorazowo, jasno ostrzec użytkownika, co
    dokładnie sprawdzić w ustawieniach telefonu.
    """

    global _oem_battery_warning_shown

    if _oem_battery_warning_shown:
        return

    try:
        for prop, label in _AGGRESSIVE_OEM_BATTERY_PROPS:

            result = execute_shell(
                "adb shell getprop " + prop,
                timeout=10
            )

            value = (result.get("stdout") or "").strip()

            if result.get("ok") and value:

                _oem_battery_warning_shown = True

                log(
                    "ANDROID",
                    "Wykryto nakładkę " + label
                    + " (" + prop + "=" + value + ")."
                )

                print()
                print(
                    "⚠️  Ten telefon używa " + label + " — ta "
                    "nakładka potrafi agresywnie zawieszać "
                    "aplikacje w tle, silniej niż standardowy "
                    "Android, i często IGNORUJE zwykłe "
                    "termux-wake-lock / 'ignoruj optymalizację "
                    "baterii'. Jeśli po przełączeniu ekranu na "
                    "inną aplikację (np. android_launch_app, "
                    "otwarcie przeglądarki) zauważysz nagłe błędy "
                    "sieciowe/DeepSeek dopóki nie wrócisz do "
                    "Termuksa — to prawdopodobnie WŁAŚNIE TO, nie "
                    "błąd agenta. Sprawdź w ustawieniach telefonu "
                    "(zwykle: Ustawienia -> Bateria -> Zarządzanie "
                    "aplikacjami w tle / Autostart) i zezwól "
                    "Termuksowi na pełną aktywność w tle."
                )
                print()

                return

    except Exception:
        pass


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

            # Pasek statusu i pasek nawigacji (com.android.systemui)
            # są identyczne w KAŻDYM kroku, niezależnie jaka aplikacja
            # jest aktywna — to czysty szum zajmujący miejsce w limicie
            # znaków, bez żadnej wartości diagnostycznej dla zespołu.
            # Odfiltrowujemy je po prefiksie pakietu w resource-id
            # (a nie po nazwie widgetu), więc działa dla KAŻDEJ
            # aplikacji, nie tylko tych zaobserwowanych w logach.
            if resource.startswith(
                _ANDROID_SYSTEMUI_RESOURCE_PREFIX
            ):
                continue

            if "/" in resource:
                resource = resource.split(
                    "/"
                )[-1]

            # Generyczne kontenery frameworka Androida/AppCompat
            # (action_bar_root, content, coordinator, root_view,
            # view_pager...) pojawiają się identycznie w PRAWIE
            # KAŻDEJ aplikacji, nie tylko w systemowym UI — nie mają
            # własnego tekstu/opisu i nie da się w nie kliknąć, więc
            # niosą zero informacji diagnostycznej, a w logach z tej
            # sesji naprawczej zajmowały większość każdego zrzutu
            # android_state. Pomijamy je TYLKO gdy faktycznie puste i
            # nieinteraktywne — jeśli kiedykolwiek mają realny tekst/
            # opis albo są klikalne/fokusowalne, zostają widoczne.
            if (
                resource in _ANDROID_GENERIC_CONTAINER_IDS
                and not text
                and not desc
                and clickable != "true"
                and focusable != "true"
            ):
                continue

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


_ANDROID_NUMERIC_NEEDLE_RE = re.compile(r"^-?\d+(\.\d+)?$")


def android_assert_text_visible(text):
    """
    Sprawdza JEDNOZNACZNIE, czy podany fragment tekstu jest
    GDZIEKOLWIEK widoczny na aktualnym ekranie — zamiast zmuszać
    Gemini do samodzielnej interpretacji długiego, czasem obciętego
    zrzutu android_summary().

    Zaobserwowany realny problem: raporty typu "wynik 19 potwierdzony
    przez android_state" okazywały się w praktyce SAMĄ DEKLARACJĄ
    Gemini po przejrzeniu dużego, obciętego zrzutu UI — nikt (ani
    Python, ani Gemini w jednoznaczny sposób) nie sprawdzał, czy "19"
    faktycznie się w nim znajduje. To narzędzie daje krótki,
    jednoznaczny fakt (found=True/False) zamiast wymagać interpretacji.

    Ograniczenie: korzysta z tego samego android_summary(), które
    obcina zrzut przy ANDROID_LIMIT znaków — fragment tekstu bardzo
    daleko w bardzo rozbudowanym drzewie UI może więc nie zostać
    znaleziony mimo że faktycznie jest na ekranie. To ten sam limit,
    z którym i tak muszą żyć MAIN/PLANNER/CRITIC czytający ten sam
    zrzut ręcznie — nie jest to nowa wada.
    """

    needle = str(text or "").strip()

    if not needle:
        return {
            "ok": False,
            "error": "Pusty tekst do sprawdzenia."
        }

    summary = android_summary()

    if summary == "Android niedostępny." or summary.startswith(
        "Android state error:"
    ):
        return {
            "ok": False,
            "error": summary
        }

    # Zgloszony realny przypadek (2026-08-23): uzytkownik na wlasne
    # oczy widzial ZLY wynik na ekranie kalkulatora, a mimo to
    # android_assert_text_visible("42") zwrocilo found=True. Zwykle
    # `in` dopasowuje krotki numeryczny needle jako PODCIAG dluzszej,
    # niepowiazanej liczby (np. "42" trafia wewnatrz "1542" albo
    # "420") — dla liczb (w tym z kropka dziesietna) wymagaj wiec
    # granic: sasiednie znaki nie moga byc cyfra ani kropka.
    if _ANDROID_NUMERIC_NEEDLE_RE.match(needle):
        pattern = (
            r"(?<![\d.])"
            + re.escape(needle)
            + r"(?![\d.])"
        )
        found = re.search(pattern, summary) is not None
    else:
        found = needle.lower() in summary.lower()

    return {
        "ok": True,
        "action": "assert_text_visible",
        "text": needle,
        "found": found
    }


# Zaobserwowany realny przypadek (2026-08-24): android_click("+")
# w kalkulatorze (com.coloros.calculator) zawiodł, a odpowiedź
# clickable_nearby jawnie pokazała, że przycisk operatora ma
# etykietę "Dodaj" (spolszczoną), nie symbol "+". Zamiast liczyć na
# to, że DeepSeek/Gemini zauważy podpowiedź i spróbuje ponownie
# (co w tym logu NIE nastąpiło — zamiast tego zespół zmarnował
# kolejną pełną rundę konsultacji + WEB_SEARCH, zanim przypadkiem
# trafił na inne podejście), spróbuj znanych, spolszczonych
# odpowiedników AUTOMATYCZNIE, zanim w ogóle zgłosisz błąd.
_ANDROID_CLICK_TEXT_SYNONYMS = {
    "+": ["Dodaj", "Plus"],
    "-": ["Odejmij", "Minus"],
    "−": ["Odejmij", "Minus"],
    "*": ["Pomnóż", "Razy", "×"],
    "x": ["Pomnóż", "Razy", "×"],
    "×": ["Pomnóż", "Razy"],
    "/": ["Podziel", "÷"],
    "÷": ["Podziel"],
    "=": ["Równa się", "Oblicz", "Wynik"],
    "c": ["Wymaż", "Wyczyść", "Kasuj", "AC", "CE"],
    "ac": ["Wymaż", "Wyczyść", "Kasuj"],
    "ce": ["Wymaż", "Wyczyść", "Kasuj", "AC"],
    "clear": ["Wymaż", "Wyczyść", "Kasuj"],
    "del": ["Backspace", "Usuń", "Wymaż"],
    "backspace": ["Usuń", "Wymaż"],
}


def android_click_text(text):
    """
    Inteligentne kliknięcie elementu Android — próbuje podanego
    tekstu, a jeśli to znany symbol/skrót matematyczny (+, -, *, /,
    =, c...), automatycznie próbuje też znanych spolszczonych
    odpowiedników (patrz _ANDROID_CLICK_TEXT_SYNONYMS) ZANIM
    zgłosi błąd — bez czekania na to, że wywołujący zauważy i
    powtórzy próbę z podpowiedzi clickable_nearby.
    """

    target_text = str(text)

    candidates = [target_text]

    for synonym in _ANDROID_CLICK_TEXT_SYNONYMS.get(
        target_text.strip().lower(), []
    ):
        if synonym not in candidates:
            candidates.append(synonym)

    result = None
    tried = []

    for candidate in candidates:

        result = _android_click_single_text(candidate)
        tried.append(candidate)

        if result.get("ok"):
            if candidate != target_text:
                result["matched_via_synonym_of"] = target_text
            return result

    if result is not None and len(tried) > 1:
        result["tried_synonyms"] = tried

    return result


def _android_click_single_text(text):
    """
    Jedna próba kliknięcia elementu Android po DOKŁADNIE podanym
    tekście (bez prób synonimów — tym zajmuje się android_click_text
    powyżej).

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

    # Zaobserwowany realny problem: gdy szukany tekst nie istnieje
    # (albo nie istnieje DOKŁADNIE tak, jak spodziewał się
    # DeepSeek/Gemini — np. etykieta jest na kontenerze, a nie na
    # klikalnym elemencie), błąd "Nie znaleziono elementu" nie daje
    # ŻADNEJ wskazówki co dalej. W realnym logu to kosztowało 3
    # dodatkowe kroki: osobny android_state, osobny pełny zrzut
    # hierarchii do pliku, osobna analiza RESEARCHERA — zanim ktoś
    # w ogóle zobaczył, jakie klikalne elementy FAKTYCZNIE są na
    # ekranie. Zbieramy je od razu przy tym samym przejściu przez
    # XML, żeby błąd sam podpowiadał, czego szukać.
    clickable_candidates = []
    seen_candidates = set()

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

            candidate_label = node_text or node_desc

            if (
                clickable == "true"
                and enabled == "true"
                and candidate_label
                and candidate_label not in seen_candidates
                and len(clickable_candidates) < 25
            ):
                seen_candidates.add(candidate_label)
                clickable_candidates.append(candidate_label)

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

    result = {
        "ok": False,
        "action": "click_text",
        "text": target,
        "error": "Nie znaleziono elementu ani jego współrzędnych."
    }

    if clickable_candidates:
        result["clickable_nearby"] = clickable_candidates
        result["hint"] = (
            "Szukany tekst '" + target + "' nie pasuje do ŻADNEGO "
            "klikalnego elementu. Powyzej sa etykiety WSZYSTKICH "
            "aktualnie klikalnych elementow na ekranie (clickable_"
            "nearby) - wybierz z nich najblizszy zamiar zamiast "
            "ponawiac ten sam nieudany tekst albo robic dodatkowy "
            "android_state/zrzut hierarchii tylko po to, zeby to "
            "sprawdzic."
        )

    return result


def android_long_click_text(text):
    """
    Długie przytrzymanie elementu Android po tekście —
    zaobserwowany realny przypadek: użytkownik ręcznie przytrzymał
    palcem pole z wpisanymi cyframi na ekranie kalkulatora bez
    zwykłego pola tekstowego i zobaczył systemowe menu z opcjami
    "Wklej" i "Wybierz wszystko" — czyli to pole JEST selekcjonowalne
    mimo że android_type nie mógł do niego nic wpisać wprost. Dla
    takich ekranów alternatywą dla klikania pojedynczych przycisków
    jest: android_set_clipboard(pełny tekst) -> android_long_click
    na aktualnie widocznym tekście pola -> android_click("Wklej") z
    wyskakującego menu (NIGDY "Udostępnij"/"Wyślij do urządzenia" z
    tego samego menu — to inna opcja, prowadzi do udostępniania, nie
    wklejania).

    Kolejność dopasowania identyczna jak w android_click_text: text,
    textContains, content-desc, XML -> bounds -> long_click na
    współrzędnych.
    """

    global android_device

    if android_device is None:
        if not init_android():
            return {
                "ok": False,
                "action": "long_click_text",
                "text": text,
                "error": "Android niedostępny."
            }

    target = str(text)

    try:
        obj = android_device(text=target)

        if obj.exists:
            obj.long_click()

            return {
                "ok": True,
                "action": "long_click_text",
                "text": target,
                "method": "text"
            }
    except Exception:
        pass

    try:
        obj = android_device(textContains=target)

        if obj.exists:
            obj.long_click()

            return {
                "ok": True,
                "action": "long_click_text",
                "text": target,
                "method": "textContains"
            }
    except Exception:
        pass

    try:
        obj = android_device(description=target)

        if obj.exists:
            try:
                obj.long_click()

                return {
                    "ok": True,
                    "action": "long_click_text",
                    "text": target,
                    "method": "content-desc"
                }
            except Exception:
                pass
    except Exception:
        pass

    visible_texts_nearby = []
    seen_texts = set()

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
            enabled = attrs.get("enabled", "true")
            bounds = attrs.get("bounds", "")

            candidate_label = node_text or node_desc

            if (
                candidate_label
                and candidate_label not in seen_texts
                and len(visible_texts_nearby) < 25
            ):
                seen_texts.add(candidate_label)
                visible_texts_nearby.append(candidate_label)

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
                android_device.long_click(x, y)

                return {
                    "ok": True,
                    "action": "long_click_text",
                    "text": target,
                    "method": "xml-bounds",
                    "x": x,
                    "y": y,
                    "bounds": bounds
                }

            except Exception:
                continue

    except Exception:
        pass

    result = {
        "ok": False,
        "action": "long_click_text",
        "text": target,
        "error": "Nie znaleziono elementu ani jego współrzędnych."
    }

    if visible_texts_nearby:
        result["visible_texts_nearby"] = visible_texts_nearby

    return result


def android_set_clipboard(text):
    """
    Ustawia systemowy schowek Androida (uiautomator2:
    device.set_clipboard()) — potrzebne do techniki
    ustaw-schowek -> długie przytrzymanie -> "Wklej" jako
    alternatywy dla klikania pojedynczych przycisków na ekranach
    bez zwykłego, edytowalnego pola tekstowego (patrz
    android_long_click_text).
    """

    if android_device is None:
        return {
            "ok": False,
            "error": "Android niedostępny"
        }

    try:

        android_device.set_clipboard(str(text))

        return {
            "ok": True,
            "action": "set_clipboard",
            "text": str(text)
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def android_paste_text(text, target_text):
    """
    Technika "ustaw schowek -> przytrzymaj -> Wklej" jako JEDNA
    atomowa operacja, zamiast trzech osobnych narzędzi, które
    dotąd trzeba było poprawnie złożyć samodzielnie — zaobserwowany
    realny przypadek: przy ręcznym składaniu tych kroków system
    pomylił kolejność / kliknął złą opcję z wyskakującego menu
    (np. "Udostępnij" zamiast "Wklej"). Ta funkcja robi wszystkie
    trzy kroki po kolei i jednoznacznie zgłasza, na którym dokładnie
    kroku coś poszło nie tak, zamiast zostawiać to zgadywaniu.

    Args:
        text: tekst do wklejenia (np. "15+27").
        target_text: aktualnie widoczny tekst pola, które trzeba
            przytrzymać, żeby wywołać menu z opcją "Wklej" (np. "0"
            dla pustego wyświetlacza kalkulatora, albo poprzednio
            wpisana wartość).
    """

    clipboard_result = android_set_clipboard(text)

    if not clipboard_result.get("ok"):
        return {
            "ok": False,
            "action": "paste_text",
            "step": "set_clipboard",
            "error": (
                "Nie udało się ustawić schowka: "
                + str(clipboard_result.get("error"))
            )
        }

    long_click_result = android_long_click_text(target_text)

    if not long_click_result.get("ok"):
        return {
            "ok": False,
            "action": "paste_text",
            "step": "long_click",
            "error": (
                "Nie udało się przytrzymać elementu '"
                + str(target_text) + "': "
                + str(long_click_result.get("error"))
            ),
            "long_click_result": long_click_result
        }

    time.sleep(0.4)

    paste_click_result = android_click_text("Wklej")

    if not paste_click_result.get("ok"):

        try:
            android_press("back")
        except Exception:
            pass

        return {
            "ok": False,
            "action": "paste_text",
            "step": "click_paste",
            "error": (
                "Menu kontekstowe nie pokazało opcji 'Wklej' (schowek "
                "może być pusty, albo ten element jednak nie jest "
                "wklejalny): " + str(paste_click_result.get("error"))
            ),
            "clickable_nearby": paste_click_result.get(
                "clickable_nearby"
            )
        }

    return {
        "ok": True,
        "action": "paste_text",
        "text": str(text),
        "target_text": str(target_text)
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


# Ścieżki wszystkich zrzutów ekranu zrobionych w TYM uruchomieniu
# (android_screenshot/android_screenshot_ocr, niezależnie od tego,
# gdzie Gemini je zapisał) — użytkownik dał jawną, trwałą zgodę na
# ich automatyczne kasowanie bez pytania o potwierdzenie, więc są
# śledzone tu osobno od _track_project_path() (ta lista dalej
# wymaga confirmu dla reszty wygenerowanych plików projektu).
# Patrz _cleanup_screenshots_silently().
_screenshot_paths_this_run = []


def _cleanup_screenshots_silently():
    """
    Kasuje WSZYSTKIE zrzuty ekranu — te z TEGO uruchomienia
    (_screenshot_paths_this_run, niezależnie od tego, gdzie Gemini
    je zapisał) oraz wszystko, co zalega w domyślnym katalogu
    AGENT_DIR/screenshots/ (np. resztki po poprzednim, twardo
    zabitym procesie) — BEZ pytania o potwierdzenie. Użytkownik dał
    na to jawną, trwałą zgodę: zrzuty to jednorazowy, nieczytany
    przez nikogo dowód wykonania, nie coś, co warto zachowywać albo
    o co warto pytać za każdym razem jak resztę wygenerowanych
    plików projektu.

    Wołane i na starcie programu (sprząta resztki sprzed ewentualnej
    awarii poprzedniego uruchomienia), i przez atexit (normalne
    zamknięcie, także Ctrl+C).
    """

    removed = 0

    for raw_path in list(_screenshot_paths_this_run):
        try:
            Path(raw_path).unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass

    _screenshot_paths_this_run.clear()

    try:
        screenshots_dir = AGENT_DIR / "screenshots"

        if screenshots_dir.is_dir():
            for p in screenshots_dir.glob("*.png"):
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass

    # Resztki sprzed wprowadzenia tego mechanizmu (v50) albo z
    # procesów zabitych PRZED zarejestrowaniem ścieżki w
    # _screenshot_paths_this_run — Gemini konsekwentnie nazywa
    # zrzuty z prefiksem "screenshot" (zgodnie z przykładami w
    # promptach), np. screenshot_krok3.png, screenshot_wikipedia.png,
    # screenshot_ustawienia.png. Sprzątamy je też, TYLKO na
    # najwyższym poziomie $HOME (nie rekurencyjnie — nie ruszamy
    # katalogów projektu).
    try:
        for p in HOME.glob("screenshot*.png"):
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    except Exception:
        pass

    if removed:
        log(
            "MAIN",
            "Usunięto " + str(removed) + " zrzutów ekranu "
            "(automatycznie, bez pytania — stała zgoda użytkownika)."
        )


atexit.register(_cleanup_screenshots_silently)


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

        # Nikt (Gemini/DeepSeek) nie odczytuje TREŚCI tego pliku —
        # użytkownik dał jawną, trwałą zgodę na automatyczne
        # kasowanie zrzutów bez pytania o potwierdzenie za każdym
        # razem (w przeciwieństwie do reszty wygenerowanych plików
        # projektu). Śledzimy więc ścieżkę tu, żeby
        # _cleanup_screenshots_silently() (atexit + start programu)
        # mogła ją posprzątać automatycznie, bez confirmu.
        _screenshot_paths_this_run.append(str(target))

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


def android_screenshot_ocr(path=None, lang=None):
    """
    Zrzut ekranu + NATYCHMIASTOWE rozpoznanie tekstu lokalnie przez
    Tesseract OCR (pkg install tesseract w Termuksie — UWAGA: pakiet
    nazywa się dokładnie "tesseract", NIE "tesseract-ocr") — ZERO
    wywołań do Gemini/innego modelu, zero kosztu limitu API,
    działa całkowicie offline na urządzeniu. Zwraca sam TEKST
    widoczny na ekranie (nie plik obrazu) — przydatne tam, gdzie
    android_state (tekstowy dump drzewa UI/accessibility) nie
    pokazuje treści, np. tekst wyrenderowany wewnątrz WebView/
    Canvas/gry, który nie ma odpowiadającego węzła accessibility.

    NIE pomoże przy weryfikacji czystej grafiki bez tekstu (kształty,
    kolory, układ elementów) — do tego nadal potrzebny jest ręczny
    podgląd zrzutu przez człowieka, nie automatyczna analiza.
    """

    screenshot_result = android_screenshot(path)

    if not screenshot_result.get("ok"):
        return screenshot_result

    target = screenshot_result["path"]

    tesseract_bin = shutil.which("tesseract")

    if not tesseract_bin:
        return {
            "ok": False,
            "error": (
                "Brak zainstalowanego 'tesseract' w Termuksie. "
                "POPRAWNA nazwa pakietu to dokładnie 'tesseract' "
                "(NIE 'tesseract-ocr' — takiego pakietu nie ma w "
                "repozytorium Termux, próba jego instalacji zawsze "
                "kończy się 'Unable to locate package'). Zainstaluj: "
                "pkg install tesseract — angielski działa od razu, "
                "bez dodatkowego pakietu językowego. Dla innych "
                "języków (np. polskiego) NIE MA osobnego pakietu do "
                "zainstalowania — trzeba ręcznie pobrać plik danych "
                "językowych, np.: curl -o "
                "$PREFIX/share/tessdata/pol.traineddata "
                "https://raw.githubusercontent.com/tesseract-ocr/"
                "tessdata/4.0.0/pol.traineddata"
            ),
            "path": target
        }

    try:
        cmd = [tesseract_bin, target, "-"]

        if lang:
            cmd += ["-l", str(lang)]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return {
                "ok": False,
                "error": (
                    "tesseract zakończył się błędem: "
                    + short(result.stderr, 500)
                ),
                "path": target
            }

        return {
            "ok": True,
            "action": "screenshot_ocr",
            "path": target,
            "text": result.stdout.strip()
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "path": target
        }


# Pakiety już pomyślnie uruchomione w BIEŻĄCYM celu (patrz
# android_launch_app niżej) — zaobserwowany realny problem: zespół
# potrafił kilkukrotnie kazać Gemini otworzyć i "potwierdzić" TĘ SAMĄ
# aplikację (np. Zegar) w różnych krokach tego samego celu, bo za
# każdym razem TASK był sformułowany innymi słowami, więc dokładne
# dopasowanie tekstu w _checklist_duplicate_message() tego nie
# łapało. Śledzimy więc powtórki po nazwie PAKIETU — jedynym
# stabilnym, ustrukturyzowanym identyfikatorze, jaki tu w ogóle mamy
# (argument narzędzia, nie wolny tekst TASK-u) — i dajemy o tym znać
# jako miękkie ostrzeżenie (nie blokadę: czasem faktyczna ponowna
# próba jest uzasadniona, np. po awarii). Czyszczone razem z resztą
# stanu celu w maybe_restart_team_sessions_for_new_goal().
_confirmed_app_launches = {}


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
        "adb shell monkey -p " + shlex.quote(package)
        + " -c android.intent.category.LAUNCHER 1",
        timeout=20
    )

    started = bool(
        result.get("ok")
        and "Events injected: 1" in result.get("stdout", "")
    )

    already_launched_note = ""

    if started:

        prior = _confirmed_app_launches.get(package)

        if prior:
            already_launched_note = (
                "Ta aplikacja (" + package + ") była już pomyślnie "
                "otwarta i potwierdzona wcześniej W TYM SAMYM CELU "
                "(pierwszy raz: " + prior + "). Jeśli to nie jest "
                "faktycznie potrzebne (np. wcześniejsza sesja z tą "
                "aplikacją padła), NIE otwieraj i nie potwierdzaj "
                "jej ponownie — to marnuje cały cykl TASK-u na coś "
                "już zrobionego. UWAGA: jeśli byłeś W TRAKCIE "
                "interakcji z tą aplikacją (np. wpisane, ale jeszcze "
                "niezatwierdzone dane), ponowne uruchomienie MOŻE "
                "zresetować jej stan do początkowego — zaobserwowany "
                "realny przypadek: wpisane cyfry w kalkulatorze "
                "zniknęły (wrócił do '0') po ponownym "
                "android_launch_app tego samego pakietu. Jeśli "
                "podejrzewasz, że apka jest po prostu w tle (nie "
                "zamknięta), sprawdź najpierw android_state zamiast "
                "od razu relaunchować."
            )

        _confirmed_app_launches[package] = datetime.now().isoformat()

    output = {
        "ok": started,
        "action": "launch_app",
        "package": package,
        "detail": short(
            result.get("stdout", "")
            + result.get("stderr", ""),
            500
        )
    }

    if already_launched_note:
        output["already_launched_note"] = already_launched_note

    return output


def android_list_packages(filter_text=None):
    """
    Lista zainstalowanych pakietów PRZEZ ADB — omija ograniczenie
    "package visibility" (Android 11+), przez które gołe `pm list
    packages` uruchomione w SAMYM Termuksie (zwykła aplikacja) widzi
    tylko ograniczony podzbiór innych zainstalowanych aplikacji, nie
    wszystkie.

    Zaobserwowany realny, powtarzający się problem: Gemini
    wielokrotnie pisał WŁASNY skrypt bash z gołym `pm list packages
    | grep ...` (czasem pamiętając o dodaniu `adb shell`, czasem
    zapominając — niezawodność promptu okazała się niewystarczająca,
    jak przy innych podobnych przypadkach w tym projekcie), który
    konsekwentnie nie znajdował aplikacji FAKTYCZNIE zainstalowanych
    na urządzeniu (Kalkulator, Zegar) — zamiast czekać, aż model
    zawsze pamięta o `adb shell`, to narzędzie robi to sam, za każdym
    razem, strukturalnie.
    """

    command = "adb shell pm list packages"

    if filter_text:
        command += " | grep -i " + shlex.quote(str(filter_text))

    result = execute_shell(command, timeout=20)

    if not result.get("ok"):
        return result

    packages = []

    for line in result.get("stdout", "").splitlines():

        line = line.strip()

        if line.startswith("package:"):
            packages.append(line[len("package:"):])

    return {
        "ok": True,
        "packages": packages,
        "count": len(packages)
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
        "adb install " + flags + " " + shlex.quote(path),
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
        "adb uninstall " + shlex.quote(package),
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
            "adb shell pidof " + shlex.quote(package),
            timeout=15
        )

        pid_out = pid_result.get("stdout", "").strip()

        if pid_out:
            pid = pid_out.split()[0]

    command = "adb logcat -d -t " + str(lines)

    if pid:
        command += " --pid=" + shlex.quote(pid)

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

        # Zaobserwowany realny incydent (POWTÓRZONY mimo wcześniejszej
        # łatki v49): użytkownik wpisał "t" na pytanie o reset sesji,
        # terminal POKAZAŁ "t", a program mimo to odczytał to jako
        # odmowę. Pierwsza próba naprawy (v49) drenowała bufor przez
        # `sys.stdin.readline()` w pętli — ale readline() BLOKUJE,
        # jeśli w buforze czeka NIEDOKOŃCZONY fragment (bez znaku
        # nowej linii, np. resztka wklejonego tekstu, której
        # użytkownik jeszcze nie "zamknął" enterem) — w tym stanie
        # KOLEJNE znaki, które użytkownik dopiero co wpisuje (prawdziwe
        # "t" + Enter), doklejają się do TEGO SAMEGO niedokończonego
        # fragmentu w buforze terminala, więc readline() zwraca
        # "resztka+t" zamiast samego "t" — terminal nadal POKAZUJE
        # wpisane "t" (lokalne echo terminala, niezależne od tego, co
        # faktycznie odczytał proces), ale porównanie z "t" już nie
        # przechodzi. Naprawione właściwym, atomowym narzędziem
        # systemowym do tego dokładnie przypadku: termios.tcflush()
        # kasuje WSZYSTKO, co czeka w buforze wejściowym terminala,
        # na poziomie kernela — bez ryzyka blokady na niedokończonej
        # linii, w przeciwieństwie do ręcznego drenażu przez readline().
        try:
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass

        answer = input(
            "   Zezwolić? [t/N] > "
        ).strip().lower()

        return answer in ("t", "tak", "y", "yes")

    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ------------------------------------------------------------
# Narzędzia Gemini/Pythona NIE SĄ plikami wykonywalnymi — nie
# istnieją w PATH Termuksa. Zaobserwowany, POWTARZAJĄCY SIĘ (v25,
# v35, v38 — trzy razy w coraz to innej postaci) wzorzec błędu:
# skrypt shell próbuje wywołać jedno z tych narzędzi bezpośrednio
# (`android_screenshot ...`) albo sprawdzić jego istnienie przez
# `command -v`/`which` — to ZAWSZE kończy się "nie znaleziono" i
# cichym przejściem na gorszy zamiennik (screencap/dumpsys), a nie
# realnym wywołaniem narzędzia. Zamiast łatać to w promptach za
# każdym razem, gdy pojawi się kolejny wariant (co robić przy
# BARDZIEJ złożonych zadaniach w przyszłości?) — blokujemy to
# STRUKTURALNIE, w kodzie, dla WSZYSTKICH tych nazw na raz, zanim
# taka komenda w ogóle zostanie wykonana. To działa niezależnie od
# tego, czy model "pamięta" regułę z promptu.
_GEMINI_ONLY_TOOL_NAMES = [
    "termux_mkdir", "termux_ls", "termux_write_file",
    "termux_read_file", "termux_run_background", "termux_processes",
    "termux_check_process", "termux_stop_process",
    "termux_start_second_session", "termux_file_exists",
    "termux_delete", "termux_check_apk", "termux_patch_file",
    "ask_deepseek",
    "chrome_tabs", "chrome_inspect", "chrome_open", "chrome_click",
    "chrome_type", "chrome_execute_js",
    "android_state", "android_click", "android_click_resource",
    "android_tap", "android_type", "android_press", "android_swipe",
    "android_long_click", "android_set_clipboard", "android_paste_text",
    "android_screenshot", "android_screenshot_ocr", "android_launch_app",
    "android_list_packages", "android_assert_text_visible",
    "android_run_in_new_window", "android_install_apk",
    "android_uninstall_app", "android_logcat",
]

_TOOL_NAMES_ALT = "|".join(
    re.escape(n)
    for n in sorted(
        _GEMINI_ONLY_TOOL_NAMES,
        key=len,
        reverse=True
    )
)

_FAKE_TOOL_EXISTENCE_CHECK_PATTERN = re.compile(
    r"\b(?:command\s+-v|which|type)\s+(" + _TOOL_NAMES_ALT + r")\b"
)

_FAKE_TOOL_BARE_INVOCATION_PATTERN = re.compile(
    r"^(?:if\s+|elif\s+|while\s+|!\s*)?(" + _TOOL_NAMES_ALT + r")\b"
)


def _looks_like_fake_tool_shell_invocation(command):
    """
    Wykrywa próbę wywołania narzędzia Gemini/Pythona jako polecenia
    powłoki (albo sprawdzenia go przez command -v/which) — analizuje
    KAŻDĄ linię osobno, bo to właśnie tak wygląda w realnych
    skryptach (jedna linia z `if command -v NAZWA`, inna z samym
    `NAZWA argumenty`). Zwraca nazwę wykrytego narzędzia albo None.
    """

    if not command:
        return None

    for line in str(command).splitlines():

        stripped = line.strip()

        if not stripped:
            continue

        match = _FAKE_TOOL_EXISTENCE_CHECK_PATTERN.search(stripped)

        if match:
            return match.group(1)

        match = _FAKE_TOOL_BARE_INVOCATION_PATTERN.match(stripped)

        if match:
            return match.group(1)

    return None


def _fake_tool_invocation_error(tool_name):
    return {
        "ok": False,
        "error": (
            "'" + tool_name + "' to narzędzie Gemini/Pythona, NIE "
            "polecenie powłoki — nie istnieje jako plik wykonywalny "
            "w PATH, więc command -v/which zawsze zwróci "
            "\"nie znaleziono\", a próba uruchomienia go w shellu "
            "zawsze się nie powiedzie. Wywołaj '" + tool_name + "' "
            "BEZPOŚREDNIO jako osobne, prawdziwe narzędzie Gemini — "
            "nie przez termux_run/termux_run_background/shell."
        ),
        "blocked_fake_tool_invocation": True
    }


# Zaobserwowany realny, wielokrotnie powtarzający się incydent:
# Gemini próbuje "przetestować" dopiero co napisane narzędzie z
# custom_tools/ przez `python -c "from agent.custom_tools.X import
# run; ..."`. To NIGDY nie może zadziałać niezależnie od __init__.py
# — `~/agent` zawiera PLIK `agent.py`, więc Python od razu traktuje
# "agent" jako zwykły MODUŁ (ten plik), nie jako pakiet z
# podkatalogami, i "agent.custom_tools" nie ma prawa się rozwiązać.
# Prompt-only ostrzeżenie (MAIN_PROMPT) okazało się niewystarczające
# — Gemini i tak próbowało tego kilka razy w jednym realnym
# przebiegu, marnując pełne cykle TASK-u na identyczny,
# przewidywalny błąd. Blokujemy to strukturalnie, z jasnym
# wyjaśnieniem DLACZEGO i co zrobić zamiast tego.
_CUSTOM_TOOL_SELF_TEST_IMPORT_PATTERN = re.compile(
    r"from\s+agent\.custom_tools\.[\w.]+\s+import"
)


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

    if _CUSTOM_TOOL_SELF_TEST_IMPORT_PATTERN.search(command):
        return {
            "ok": False,
            "error": (
                "Import 'from agent.custom_tools.X import ...' NIGDY "
                "nie zadziała z tego katalogu — '~/agent' zawiera "
                "PLIK agent.py, więc Python od razu traktuje 'agent' "
                "jako zwykły moduł (ten plik), nie jako pakiet z "
                "podkatalogami, niezależnie od __init__.py. Narzędzie "
                "z custom_tools/ jest już zarejestrowane AUTOMATYCZNIE "
                "od razu przy zapisie (termux_write_file) — wywołaj "
                "je BEZPOŚREDNIO jako zwykłe narzędzie w NASTĘPNEJ "
                "konsultacji, nie testuj go przez import w Pythonie."
            ),
            "command": command,
            "blocked_by_safety_gate": True
        }

    fake_tool = _looks_like_fake_tool_shell_invocation(command)

    if fake_tool:
        return _fake_tool_invocation_error(fake_tool)

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
            executable=_SHELL_EXECUTABLE,
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

# Zaobserwowany realny problem: gdy CDP raz się zepsuje w trakcie
# celu (np. karta Chrome nawiguje gdzie indziej/proces pada),
# chrome_summary() wywoływane RAZ NA KROK i tak zawsze próbuje
# ensure_chrome_cdp_forward() od nowa z pełnymi 3 próbami x 1s
# przerwy — kilka sekund straconych na każdym KOLEJNYM kroku, mimo
# że wynik ("Brak dostępnych kart Chrome/CDP") jest z góry znany.
# Licznik porażek z rzędu ogranicza liczbę prób po 2. porażce,
# zamiast przestać próbować całkowicie — telefon może w
# międzyczasie odzyskać CDP (np. użytkownik ręcznie otworzy kartę).
_CDP_CONSECUTIVE_FAILURES = 0


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

    Po 2 porażkach Z RZĘDU liczba prób jest ograniczana do 1 (bez
    utraty samej zdolności do wykrycia powrotu CDP) — patrz
    _CDP_CONSECUTIVE_FAILURES powyżej.
    """

    global _CDP_CONSECUTIVE_FAILURES

    if _CDP_CONSECUTIVE_FAILURES >= 2:
        retries = 1

    device = find_adb()

    if not device:
        log("CHROME", "ADB: brak urządzenia")
        _CDP_CONSECUTIVE_FAILURES += 1
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

            _CDP_CONSECUTIVE_FAILURES = 0

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

    _CDP_CONSECUTIVE_FAILURES += 1

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

    # Zaobserwowany realny problem: chrome_open/chrome_click/
    # chrome_type mają tab_id/contains jako OPCJONALNE parametry w
    # schemacie narzędzia (gemini_tools()) — Gemini w praktyce
    # prawie zawsze woła je z SAMYM url/text, bez tab_id/contains
    # (nie zna z góry opaque tab_id, a rzadko podaje contains, gdy
    # nic go o tym nie uprzedza). Bez tej reguły find_tab(None,
    # None) zwracał None BEZWARUNKOWO, więc chrome_open zawodziło
    # ZAWSZE — nawet gdy istniała dokładnie jedna, oczywista karta
    # do użycia ("[831] Nowa karta"). Gdy jest dokładnie jedna
    # karta, nie ma żadnej niejednoznaczności — użyj jej.
    if not tab_id and not contains and len(tabs) == 1:
        return tabs[0]

    return None


# Na wyraźną prośbę użytkownika (2026-08-28): najsłabszym punktem
# obsługi Chrome był NASZ WŁASNY, ręcznie pisany JSON-RPC po surowym
# websockecie — to tam realnie pojawiały się błędy w logach ("CDP
# próba 1/3 nieudana", "CDP niedostępne"). Sprawdzone PRZED
# wdrożeniem (nauczka z pomyłki przy pydantic): `pychrome` używa
# WYŁĄCZNIE `requests` i `websocket-client` — tych samych dwóch
# bibliotek, których ten plik już używa — więc zero nowego ryzyka
# platformowego na Termux (w odróżnieniu od np. Playwrighta, który
# oficjalnie NIE wspiera Androida).
#
# `cdp_connect()`/`cdp_call()` zachowują DOKŁADNIE ten sam kontrakt
# co wcześniej (ten sam zwracany kształt, to samo `ws.close()` w
# `finally` u wywołujących) — dzięki temu chrome_eval() i chrome_open()
# (jedyne dwa miejsca, które ich bezpośrednio używają) NIE MUSIAŁY
# zostać w ogóle zmienione. `_PychromeConnection` to cienki wrapper
# zapewniający zgodność z `ws.close()`, którego wywołujący już
# oczekują, mimo że pychrome.Tab ma metodę `stop()`, nie `close()`.
class _PychromeConnection:

    def __init__(self, tab):
        self._tab = tab

    def close(self):
        try:
            self._tab.stop()
        except Exception:
            pass


def cdp_connect(tab):

    try:

        pychrome_tab = pychrome.Tab(**tab)
        pychrome_tab.start()

        return _PychromeConnection(pychrome_tab)

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

    try:

        result = ws._tab.call_method(
            method,
            _timeout=timeout,
            **(params or {})
        )

        return {
            "ok": True,
            "result": result
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def chrome_eval(
    tab,
    javascript,
    timeout=20
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
            },
            timeout=timeout
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
    # NIE TWORZYMY NOWEJ KARTY przez CDP (/json/new nie jest
    # używane — patrz komentarz przy CDP_403). Ale zamiast od razu
    # poddawać się, gdy nie ma pasującej karty CDP, próbujemy
    # NATYWNEJ drogi Androida (am start -a VIEW -d <url>) — to
    # dokładnie ta sama komenda, którą MAIN_PROMPT każe zespołowi
    # wykonać RĘCZNIE jako obejście; robimy to tu automatycznie, bo
    # zaobserwowany realny problem to zespół WIEDZĄCY o tym
    # obejściu, ale nie wykonujący go konsekwentnie za każdym razem
    # (chrome_open zawodził 4x pod rząd zamiast raz przełączyć się
    # na am start).
    #
    # KRYTYCZNE — zaobserwowany realny incydent: goły `am start -a
    # VIEW -d <url>` BEZ pakietu pozwala Androidowi wybrać DOWOLNĄ
    # aplikację obsługującą ten intent — na tym urządzeniu czasem
    # otworzył Firefoksa zamiast Chrome. CDP jest podłączone
    # WYŁĄCZNIE do Chrome (adb forward tcp:9222), więc karta otwarta
    # w innej przeglądarce jest dla chrome_tabs() całkowicie
    # niewidoczna, niezależnie od tego, ile razy spróbujemy. Wymuszamy
    # więc pakiet Chrome (-p com.android.chrome); jeśli akurat go nie
    # ma na tym urządzeniu (inny build/wariant Chrome), próbujemy
    # jeszcze raz bez wymuszenia pakietu jako ostatniej deski ratunku.
    if tab is None:

        fallback = execute_shell(
            "am start -a android.intent.action.VIEW -p "
            "com.android.chrome -d "
            + shlex.quote(str(url))
        )

        if not fallback.get("ok"):

            fallback = execute_shell(
                "am start -a android.intent.action.VIEW -d "
                + shlex.quote(str(url))
            )

        if not fallback.get("ok"):

            return {
                "ok": False,
                "error": (
                    "Nie znaleziono istniejącej karty CDP, a "
                    "próba otwarcia przez 'am start' też się nie "
                    "powiodła: "
                    + str(fallback.get("error", fallback))
                ),
                "fallback_attempted": "am_start"
            }

        time.sleep(2.0)

        domain = ""

        try:
            domain = urlparse(str(url)).netloc
        except Exception:
            pass

        tab = (
            find_tab(None, domain or None)
            or find_tab(None, None)
        )

        if tab is None:

            return {
                "ok": False,
                "error": (
                    "Wysłano intencję 'am start' dla adresu, ale "
                    "nadal brak widocznej karty CDP (domyślna "
                    "przeglądarka mogła nie być Chrome, albo CDP "
                    "jeszcze nie zaindeksowało nowej karty — "
                    "sprawdź android_state, żeby potwierdzić, co "
                    "faktycznie jest na ekranie)."
                ),
                "fallback_attempted": "am_start"
            }

        return {
            "ok": True,
            "tab_id": tab["id"],
            "url": tab["url"],
            "title": tab["title"],
            "method": "am_start_fallback"
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
# CHROME EXECUTE JS
# ============================================================
#
# Zaobserwowany realny problem (log 2026-08-26, cel: połączenie
# głosowe przez Vapi Dashboard): zespół POPRAWNIE zdiagnozował, że
# chrome_inspect() tylko CZYTA DOM i nie wykonuje JavaScriptu, i
# POPRAWNIE napisał kod (fetch() do API Vapi z "credentials:
# 'include'", korzystający z sesji przeglądarki, w którą użytkownik
# WŁAŚNIE się zalogował) — ale w całym zestawie narzędzi nie było
# NIC, co pozwoliłoby ten kod faktycznie wykonać. Zamiast tego zespół
# kliknął przypadkowy przycisk demo ("Test Talk to my agent" — to
# wbudowana funkcja Vapi do testowania asystenta przez mikrofon w
# przeglądarce, NIE prawdziwe połączenie wychodzące) i zgłosił to
# jako dowód sukcesu — CRITIC to poprawnie zablokował za każdym
# razem, ale zespół nie miał ŻADNEJ innej drogi do przodu, więc
# wracał do tego samego, błędnego działania w kółko.
#
# chrome_eval() (mechanizm CDP "Runtime.evaluate") już od dawna
# istniał w kodzie i jest używany WEWNĘTRZNIE przez chrome_click/
# chrome_type — ale nigdy nie był wystawiony Gemini jako osobne,
# ogólne narzędzie z DOWOLNYM kodem JS. To domyka lukę: Gemini może
# teraz faktycznie wykonać fetch()/XHR w kontekście zalogowanej
# karty (z prawdziwymi ciasteczkami sesji), zamiast pisać taki kod
# do pliku, którego i tak nic nie uruchomi.

def chrome_execute_js(
    javascript,
    tab_id=None,
    contains=None
):
    """
    Zaobserwowany realny problem (log 2026-08-27, cel: klucz API
    Bland AI): Gemini po utworzeniu nowego klucza próbował go odczytać
    przez coś na wzór navigator.clipboard.readText() (strona pokazywała
    "API key copied to clipboard") — ale odczyt schowka przeglądarki
    wymaga zgody użytkownika w oknie, którego NIKT (żaden proces) nie
    może kliknąć w tej sesji CDP. Wywołanie wisiało pełne 20 sekund
    (domyślny timeout cdp_call) i kończyło się "Connection timed out",
    po czym zespół, zamiast spróbować odczytać wartość WPROST Z DOM-u
    (np. z pola/input/modału, w którym nowy klucz jest widoczny w
    postaci jawnego tekstu ZANIM zniknie po jego "skopiowaniu"),
    poddawał się i pytał człowieka.

    Krótszy timeout (10s zamiast domyślnych 20s dla chrome_click/
    chrome_type/chrome_inspect, które używają wyłącznie szybkich,
    znanych operacji DOM) sprawia, że taka sytuacja kończy się
    szybciej i jaśniejszym błędem, zamiast marnować pół minuty na
    każdą próbę.
    """

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

    result = chrome_eval(
        tab,
        javascript,
        timeout=10
    )

    if (
        isinstance(result, dict)
        and result.get("ok") is False
    ):
        return result

    return {
        "ok": True,
        "value": result
    }


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
            "description": (
                "Zapisz plik bezpośrednio w Termuxie. Używaj do dużego "
                "kodu. UWAGA: domyślnie (append=false/pominięte) "
                "NADPISUJE cały plik — jeśli plik już ma treść z "
                "wcześniejszych kroków TEGO SAMEGO celu (np. wspólny "
                "raport, do którego kolejne punkty dopisują dowody), "
                "użyj append=true, żeby dopisać na koniec zamiast "
                "skasować to, co już tam jest."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "append": {
                        "type": "boolean",
                        "description": (
                            "true = dopisz na koniec pliku, nie "
                            "ruszając istniejącej treści. false/"
                            "pominięte = nadpisz cały plik (domyślne, "
                            "jak dotychczas)."
                        )
                    }
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
            "name": "chrome_execute_js",
            "description": (
                "Wykonaj DOWOLNY kod JavaScript w kontekście istniejącej "
                "karty Chrome (przez CDP) i zwróć jego wynik. Kod działa "
                "z PRAWDZIWĄ sesją/ciasteczkami tej karty — np. fetch() "
                "do API strony, do której użytkownik jest już zalogowany "
                "w przeglądarce, zadziała tak samo jak w konsoli "
                "deweloperskiej. Użyj tego, gdy chrome_click/chrome_type "
                "nie wystarczą (np. trzeba wywołać wewnętrzne API strony "
                "bezpośrednio) — NIE pisz takiego kodu do pliku .js, bo "
                "nic go tam nie uruchomi. NIGDY nie używaj "
                "navigator.clipboard.readText()/writeText() — wymaga "
                "zgody w oknie, którego nikt nie może kliknąć, i wisi aż "
                "do timeoutu (10s). Gdy strona pokazuje nowo utworzoną "
                "wartość (np. klucz API) i mówi 'skopiowano do "
                "schowka' — odczytaj ją WPROST z DOM (np. "
                "document.querySelector(...).value/innerText tego pola/"
                "modału), zanim znika po zamknięciu, zamiast próbować "
                "czytać systemowy schowek."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "javascript": {"type": "string"},
                    "tab_id": {"type": "string"},
                    "contains": {"type": "string"}
                },
                "required": ["javascript"]
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
            "name": "android_long_click",
            "description": (
                "Długie przytrzymanie elementu Androida po tekście "
                "(np. 1-2 sekundy). Używaj do wywołania systemowego "
                "menu zaznaczania/wklejania na polach, które nie mają "
                "zwykłej klawiatury do wpisywania, ale reagują na "
                "przytrzymanie (zaobserwowany realny przypadek: "
                "wyświetlacz kalkulatora bez pola tekstowego pokazał "
                "po przytrzymaniu opcje 'Wklej' i 'Wybierz wszystko'). "
                "Po otwarciu tego menu kliknij WYŁĄCZNIE 'Wklej' "
                "(android_click) — nigdy 'Udostępnij'/'Wyślij do "
                "urządzenia' z tego samego menu, to inna, niechciana "
                "opcja. Zobacz też android_set_clipboard."
            ),
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
            "name": "android_set_clipboard",
            "description": (
                "Ustaw systemowy schowek Androida na podany tekst. "
                "Zwykle NIE wołaj tego osobno — użyj od razu "
                "android_paste_text, które robi to razem z resztą "
                "kroków w jednym, niezawodnym wywołaniu. Ten tzw. "
                "surowy krok zostaw sobie tylko na sytuacje, gdy "
                "faktycznie potrzebujesz kontroli nad każdym krokiem "
                "z osobna."
            ),
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
            "name": "android_paste_text",
            "description": (
                "Wklej tekst na ekran bez pola tekstowego do zwykłego "
                "wpisywania (np. niektóre kalkulatory) w JEDNYM, "
                "niezawodnym kroku: ustawia schowek, przytrzymuje "
                "pole (target_text — aktualnie widoczny tekst tego "
                "pola, np. \"0\" dla pustego wyświetlacza), klika "
                "WYŁĄCZNIE 'Wklej' z wyskakującego menu (nigdy "
                "'Udostępnij'/'Wyślij do urządzenia' — to inna "
                "opcja) i zwraca jasną informację, na którym kroku "
                "coś ewentualnie poszło nie tak. To PIERWSZY WYBÓR "
                "zamiast ręcznego składania android_set_clipboard + "
                "android_long_click + android_click(\"Wklej\") "
                "osobno — mniej okazji do pomyłki."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "target_text": {"type": "string"}
                },
                "required": ["text", "target_text"]
            }
        },

        {
            "type": "function",
            "name": "android_type",
            "description": (
                "Wpisz tekst do aktywnego, EDYTOWALNEGO pola tekstowego "
                "(np. pasek wyszukiwania, pole formularza, notatka). "
                "Wymaga, żeby na ekranie faktycznie było skupione pole "
                "tekstowe — jeśli go nie ma (np. wiele kalkulatorów i "
                "klawiatur numerycznych to zwykłe PRZYCISKI bez żadnego "
                "pola tekstowego), tekst może nie trafić NIGDZIE albo "
                "zniknąć bez efektu, ALBO wywołać systemowe menu "
                "zaznaczania/udostępniania Androida (przycisk "
                "'Udostępnij', 'Wyślij do urządzenia', Bluetooth, "
                "e-mail — zaobserwowany realny przypadek: użytkownik "
                "zobaczył to na żywo na ekranie). Jeśli to się stanie, "
                "NATYCHMIAST android_press('back') żeby to zamknąć — "
                "NIE próbuj klikać niczego w tym menu. Dla ekranów bez "
                "prawdziwego pola tekstowego użyj zamiast android_type "
                "narzędzia android_click po tekście KAŻDEGO przycisku "
                "z osobna (np. \"1\", \"5\", \"+\", \"2\", \"7\", \"=\")."
            ),
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
            "name": "android_screenshot_ocr",
            "description": (
                "Zrzut ekranu + NATYCHMIASTOWE rozpoznanie widocznego "
                "tekstu lokalnie przez Tesseract OCR — zero wywołań "
                "do Gemini/innego modelu, zero kosztu limitu, "
                "działa offline. Zwraca sam TEKST (nie obraz) — "
                "używaj tam, gdzie android_state (drzewo UI) nie "
                "pokazuje treści, np. tekst wewnątrz WebView/Canvas/"
                "gry bez węzła accessibility. NIE pomoże przy "
                "weryfikacji czystej grafiki bez tekstu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "lang": {
                        "type": "string",
                        "description": (
                            "Kod języka Tesseract, np. 'eng' albo "
                            "'pol' (wymaga zainstalowanego pakietu "
                            "danych języka). Domyślnie eng."
                        )
                    }
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
            "name": "android_list_packages",
            "description": (
                "Wyszukaj nazwę pakietu zainstalowanej aplikacji "
                "PRZEZ ADB (widzi WSZYSTKIE zainstalowane aplikacje, "
                "w odróżnieniu od gołego 'pm list packages' w "
                "skrypcie bash, które może nic nie znaleźć nawet "
                "dla aplikacji faktycznie obecnej na urządzeniu — "
                "ograniczenie systemowe Androida 11+). Używaj tego "
                "PRZED android_launch_app, żeby ustalić dokładną "
                "nazwę pakietu (np. Kalkulatora, Zegara) zamiast "
                "zgadywać popularne nazwy typu com.android.calculator2 "
                "— na wielu urządzeniach (OEM-owe ROM-y) są inne."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_text": {
                        "type": "string",
                        "description": (
                            "Fragment nazwy do wyszukania, np. "
                            "'calc' albo 'clock' (bez tego zwraca "
                            "WSZYSTKIE zainstalowane pakiety)."
                        )
                    }
                }
            }
        },

        {
            "type": "function",
            "name": "android_assert_text_visible",
            "description": (
                "Sprawdź JEDNOZNACZNIE, czy podany fragment tekstu "
                "(np. wynik działania '19', nazwa ekranu 'Alarm') "
                "jest GDZIEKOLWIEK widoczny na aktualnym ekranie — "
                "zwraca krótkie true/false zamiast zmuszać Cię do "
                "samodzielnego czytania i interpretowania całego, "
                "długiego zrzutu android_state. UŻYWAJ TEGO do "
                "potwierdzania konkretnych wyników/wartości zamiast "
                "wołać android_state i oceniać 'na oko' — to jedyny "
                "sposób, żeby Twoje potwierdzenie było FAKTYCZNYM "
                "dowodem, nie tylko Twoją deklaracją."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Dokładny fragment tekstu do wyszukania "
                            "na ekranie, np. '19' albo 'Zapisano'."
                        )
                    }
                },
                "required": ["text"]
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


def termux_write_file(path, content, append=False):
    """
    append (opcjonalny, domyślnie False — zachowuje dotychczasowe
    zachowanie): False = nadpisz cały plik (jak zawsze); True =
    dopisz na koniec, nie ruszając istniejącej treści.

    Zaobserwowany realny problem (log 2026-08-25): cel wymagał
    zapisywania dowodów DLA KOLEJNYCH punktów do TEGO SAMEGO pliku w
    osobnych krokach. Jeden z kroków wywołał termux_write_file bez
    żadnej opcji dopisywania (bo jej po prostu nie było) — nadpisał
    plik nową, krótką treścią, kasując po cichu dowód poprzedniego
    punktu zapisany kilka kroków wcześniej. PLANNER zauważył to
    dopiero w NASTĘPNYM kroku i musiał odtwarzać utracone dane —
    to nie był błąd DeepSeeka, tylko brak narzędzia w Pythonie,
    które w ogóle umożliwiałoby bezpieczne dopisywanie.
    """

    try:
        p = Path(str(path)).expanduser()

        p.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        data = str(
            content if content is not None else ""
        )

        previous_size = None

        if p.exists():
            try:
                previous_size = p.stat().st_size
            except Exception:
                previous_size = None

        if append and p.exists():

            with p.open("a", encoding="utf-8") as f:
                f.write(data)

            new_size = p.stat().st_size

        else:

            p.write_text(
                data,
                encoding="utf-8"
            )

            new_size = len(data.encode("utf-8"))

        _track_project_path(p)

        result = {
            "ok": True,
            "path": str(p),
            "bytes": new_size
        }

        # Sygnał ostrzegawczy zamiast cichej utraty danych: nadpisanie
        # (nie dopisanie) pliku, który miał już niebagatelną zawartość,
        # nowszą treścią WYRAŹNIE mniejszą niż poprzednia, to mocny
        # sygnał przypadkowego skasowania wcześniejszego dowodu, a nie
        # celowej podmiany — MAIN/PLANNER dowiadują się o tym OD RAZU
        # w tym samym kroku, nie kilka kroków później przez detektywistykę.
        if (
            not append
            and previous_size is not None
            and previous_size > 200
            and new_size < previous_size * 0.5
        ):
            result["warning"] = (
                "UWAGA: ten zapis NADPISAŁ istniejący plik (miał "
                + str(previous_size) + " B, teraz ma " + str(new_size)
                + " B) — jeśli zawierał dowody z wcześniejszych kroków "
                "tego samego celu, mogły zostać właśnie SKASOWANE. "
                "Jeśli chodziło o DOPISANIE, użyj append=true zamiast "
                "nadpisywania całego pliku."
            )

        # Niespójność naprawiona: termux_patch_file od dawna
        # sprawdza py_compile po każdej edycji .py i zwraca
        # dokładny błąd (plik+linia+treść) bez potrzeby ponownego
        # czytania całego pliku — ale termux_write_file (używane
        # gdy Gemini pisze kod OD ZERA, nie edytuje fragment) nie
        # sprawdzało NIC. Plik z błędem składni zapisywał się po
        # cichu, a błąd wychodził dopiero przy próbie uruchomienia,
        # zmuszając Gemini do zgadywania na podstawie tracebacka
        # zamiast dostać dokładne miejsce od razu. Nie cofamy pliku
        # (w przeciwieństwie do patcha — tu często nie ma do czego
        # wracać, bo plik jest nowy) — tylko jawnie sygnalizujemy
        # błąd i jego dokładną lokalizację.
        if p.suffix == ".py":

            try:
                compile_check = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "py_compile",
                        str(p)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20
                )

                if compile_check.returncode != 0:

                    result["ok"] = False
                    result["compile_error"] = short(
                        compile_check.stderr,
                        1500
                    )
                    result["warning"] = (
                        "Plik ZOSTAŁ zapisany na dysku, ale NIE "
                        "PRZECHODZI py_compile — zawiera błąd "
                        "składni (patrz compile_error: dokładny "
                        "plik, numer linii i treść błędu). Plik "
                        "NIE został cofnięty (w przeciwieństwie do "
                        "termux_patch_file) — popraw dokładnie "
                        "wskazaną linię, zanim spróbujesz go "
                        "uruchomić."
                    )

            except Exception:
                # py_compile samo w sobie niedostępne — nie
                # blokujemy zapisu z tego powodu.
                pass

            # Zaobserwowany realny problem: Gemini zapisał
            # ~/agent/custom_tools/sumator.py bez TOOL_NAME/
            # TOOL_DESCRIPTION/TOOL_PARAMETERS (kontrakt z
            # MAIN_PROMPT), więc load_custom_tools() PO CICHU
            # odrzucił plik (log "[CUSTOM_TOOL] ODRZUCONO...") —
            # ale to zdarzenie żyło WYŁĄCZNIE w surowym logu
            # terminala, nigdy w wyniku TEGO wywołania narzędzia.
            # Task zgłosił się jako COMPLETED, a MAIN/zespół nigdy
            # się nie dowiedzieli, że "nowe narzędzie" faktycznie
            # nigdy nie zostało zarejestrowane. Walidujemy więc
            # kontrakt OD RAZU przy zapisie do custom_tools/, tym
            # samym mechanizmem co load_custom_tools(), i od razu
            # rejestrujemy plik, jeśli przechodzi — bez czekania na
            # "następną konsultację".
            try:
                if p.resolve().is_relative_to(
                    CUSTOM_TOOLS_DIR.resolve()
                ):

                    tool_name, load_result = _load_one_custom_tool(p)

                    if tool_name is None:

                        result["custom_tool_rejected"] = load_result

                        log(
                            "CUSTOM_TOOL",
                            "ODRZUCONO " + p.name + ": "
                            + str(load_result)
                        )

                    else:

                        CUSTOM_TOOLS[tool_name] = load_result

                        try:
                            _custom_tool_file_state[str(p)] = (
                                p.stat().st_mtime
                            )
                        except Exception:
                            pass

                        result["custom_tool_registered"] = tool_name

                        log(
                            "CUSTOM_TOOL",
                            "Załadowano nowe narzędzie: "
                            + tool_name + " (" + p.name + ")"
                        )

            except Exception:
                pass

        return result

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


# Moment startu TEGO uruchomienia agenta. Ustawiany raz, przy
# imporcie modułu — służy WYŁĄCZNIE do rozpoznania, czy odczytywany
# plik powstał w tej sesji, czy został po poprzedniej (patrz
# termux_read_file).
_SESSION_STARTED_AT = time.time()


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

        result = {
            "ok": True,
            "path": str(p),
            "content": data.decode(
                "utf-8",
                errors="replace"
            ),
            "truncated": truncated
        }

        # ZAOBSERWOWANY REALNY PROBLEM (log 2026-08-28, cel "zadzwoń
        # do Beaty", KROK 12): Gemini odczytało ~/agent/.env
        # pozostawiony przez POPRZEDNIĄ sesję i potraktowało jego
        # treść jako aktualną — a plik zawierał
        # "VAPI_API_KEY=pk-mock-valid-key-for-tests" (klucz-atrapa z
        # testów) ORAZ "BEATA_PHONE=+48500600700", czyli numer INNY
        # niż prawdziwy numer podany przez użytkownika w TEJ sesji
        # (+48514590110). W tym samym kroku Gemini odczytało też
        # call_result.txt i call_test.log z dawnych sesji jako "stan
        # obecny". Sprzątanie po sesji tego nie łapie: śledzone są
        # tylko pliki utworzone przez termux_write_file/
        # termux_patch_file/write_engineer_code_to, a te powstały
        # przez zwykłe przekierowanie powłoki (`curl ... > plik`),
        # więc nigdy nie trafiły na listę do wyczyszczenia.
        # Świadomie NIE kasujemy tu niczego automatycznie (skanowanie
        # katalogu domowego zostało wcześniej wycofane po incydencie,
        # w którym usunęło ~/api_token.txt) — zamiast tego jawnie
        # OZNACZAMY plik jako pochodzący sprzed startu tej sesji,
        # deterministycznie, na podstawie czasu modyfikacji.
        try:
            mtime = p.stat().st_mtime

            if mtime < _SESSION_STARTED_AT:
                result["stale_from_previous_session"] = True
                result["modified"] = datetime.fromtimestamp(
                    mtime
                ).strftime("%Y-%m-%d %H:%M:%S")
                result["stale_warning"] = (
                    "UWAGA: ten plik powstał PRZED startem bieżącej "
                    "sesji (ostatnia zmiana: "
                    + result["modified"]
                    + "), więc pochodzi z POPRZEDNIEGO uruchomienia "
                    "agenta. Jego treść może być nieaktualna albo "
                    "testowa (zaobserwowany realny przypadek: stary "
                    ".env z kluczem-atrapą i INNYM numerem telefonu "
                    "niż podany w tej sesji). Zanim się na nim "
                    "oprzesz, potwierdź te dane wobec tego, co "
                    "ustalono W TEJ sesji."
                )
        except Exception:
            pass

        return result

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


# Zaobserwowany realny bug (log 2026-08-28, cel: zadzwoń do Beaty
# przez Bland AI): Gemini zbudował
#   curl ... -H 'Authorization: Bearer $API_KEY' ...
# — czyli $API_KEY W POJEDYNCZYCH CUDZYSŁOWACH. W bashu pojedyncze
# cudzysłowy NIE rozwijają zmiennych — do Bland poleciał DOSŁOWNY
# tekst "Bearer $API_KEY", nie prawdziwy klucz, stąd
# {"error":"AUTH_FAILURE","message":"Unauthorized"} mimo że
# użytkownik wcześniej wprost wkleił poprawny klucz. Co gorsza,
# `curl` bez flagi `-f` zwraca kod wyjścia 0 nawet przy odpowiedzi
# HTTP 401/403/500 — więc "ok": true, returncode 0 — TASK zgłosił się
# jako COMPLETED, a błąd wyszedł na jaw dopiero gdy ktoś przeczytał
# treść pliku z odpowiedzią. Ten dokładny wzorzec (`-H`/nagłówek albo
# dowolny argument z $ZMIENNĄ zamknięty w POJEDYNCZYCH cudzysłowach)
# jest wykrywalny statycznie z samej treści komendy, ZANIM się ją
# wykona — i jest uniwersalny: dotyczy każdego przyszłego zadania,
# które buduje polecenie curl/API z kluczem/tokenem w zmiennej powłoki.
_SHELL_VAR_RE = re.compile(r'\$\{?[A-Za-z_][A-Za-z0-9_]*\}?')


def _detect_single_quoted_shell_variable(command):

    for quoted in re.findall(r"'([^']*)'", str(command or "")):

        match = _SHELL_VAR_RE.search(quoted)

        if match:
            return (
                "Komenda zawiera zmienną powłoki '" + match.group(0)
                + "' WEWNĄTRZ pojedynczych cudzysłowów ('...') — bash "
                "NIE rozwija zmiennych w pojedynczych cudzysłowach, "
                "więc do zdalnego serwisu/pliku poleciałby DOSŁOWNY "
                "tekst '" + match.group(0) + "', nie jej wartość "
                "(dokładnie to spowodowało AUTH_FAILURE przy "
                "prawdziwym kluczu API w przeszłości). Użyj podwójnych "
                "cudzysłowów (\"...\") tam, gdzie potrzebna jest "
                "wartość zmiennej."
            )

    return None


# "Rejestracja warstw" (na wyraźną prośbę użytkownika, allegoria
# CMYK/sitodruk — każda warstwa/krok musi się dokładnie pokryć z
# tym, co wyprodukowała poprzednia): krok N zapisuje plik, krok N+1
# ZAKŁADA, że on tam jest i czyta go przez `$(cat ~/plik)`. Jeśli
# krok N faktycznie nie zapisał go pod TĄ dokładną ścieżką (literówka,
# inny katalog, cichy wcześniejszy błąd) — samo podstawienie `$(cat
# ...)` nie wywala polecenia: `cat` zgłosi błąd na stderr, ale
# podstawienie i tak "zadziała", tylko z PUSTYM tekstem zamiast
# prawdziwej wartości (dokładnie ten sam rodzaj cichej, niewidocznej
# awarii co pojedyncze cudzysłowy w _detect_single_quoted_shell_
# variable — inna przyczyna, ten sam objaw: kolejny krok dostaje
# śmieciowe dane, a "ok": true nic o tym nie mówi). Wykrywamy to
# STATYCZNIE, zanim polecenie w ogóle ruszy.
_CAT_SUBSTITUTION_RE = re.compile(
    r'\$\(\s*cat\s+([^\s)]+)\s*\)|`\s*cat\s+([^\s`]+)\s*`'
)


def _detect_missing_cat_substitution_file(command):

    command_str = str(command or "")

    for m in _CAT_SUBSTITUTION_RE.finditer(command_str):

        raw_path = m.group(1) or m.group(2)

        # Plik tworzony W TEJ SAMEJ komendzie (np. "echo x > ~/f &&
        # cat $(cat ~/f)") to samodzielna, kompletna komenda, nie
        # "niezarejestrowana warstwa" między dwoma krokami — pomijamy.
        if re.search(
            r'>>?\s*["\']?' + re.escape(raw_path) + r'["\']?(\s|$|;|&)',
            command_str
        ):
            continue

        try:
            expanded = Path(raw_path).expanduser()
        except Exception:
            continue

        if not expanded.exists():
            return (
                "Komenda odczytuje plik '" + raw_path + "' przez "
                "podstawienie $(cat ...), ale ten plik NIE ISTNIEJE "
                "na dysku w tej chwili — podstawienie zwróci PUSTY "
                "tekst bez żadnego widocznego błędu polecenia (samo "
                "'cat' zgłosi błąd na stderr, ale reszta polecenia i "
                "tak 'zadziała' z pustą wartością zamiast prawdziwej). "
                "Sprawdź, czy poprzedni krok faktycznie zapisał ten "
                "plik pod DOKŁADNIE tą ścieżką, zanim uznasz że dane "
                "są gotowe do użycia."
            )

    return None


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
                    "tej samej komendy ponownie przez termux_run. "
                    "UWAGA: 'log_file' powyżej jest UNIKALNY dla "
                    "TEGO wywołania — nie myl go z log_file z "
                    "poprzednich komend w tym kroku/zadaniu. Jeśli "
                    "od uruchomienia minęło kilka sekund, plik może "
                    "być jeszcze pusty albo zawierać tylko "
                    "początkowe komunikaty — to NIE oznacza błędu "
                    "ani zakończenia, poczekaj (termux_check_"
                    "process) i sprawdź log_file ponownie, zanim "
                    "cokolwiek zgłosisz jako wynik."
                )

            return bg

        result = execute_shell(command_str)

        if isinstance(result, dict):

            quoting_warning = _detect_single_quoted_shell_variable(
                command_str
            )

            if quoting_warning:
                result["shell_quoting_warning"] = quoting_warning

            missing_file_warning = _detect_missing_cat_substitution_file(
                command_str
            )

            if missing_file_warning:
                result["missing_file_warning"] = missing_file_warning

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

        fake_tool = _looks_like_fake_tool_shell_invocation(command)

        if fake_tool:
            return _fake_tool_invocation_error(fake_tool)

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
            # WCZEŚNIEJ: stała nazwa "agent_background.log" dla
            # KAŻDEJ komendy w tle, otwierana w trybie append. To
            # oznaczało, że nowa komenda dopisywała się na końcu
            # pliku po wyniku POPRZEDNIEJ, niepowiązanej komendy —
            # jeśli Gemini odczytał log_file sekundy po starcie
            # (zanim nowa komenda zdążyła cokolwiek wypisać),
            # widział WYŁĄCZNIE stare dane i błędnie wnioskował
            # np. "BUILD FAILED" na podstawie logu sprzed kilku
            # kroków. Realny przypadek: gradlew assembleDebug
            # uruchomiony o 17:57:34, log odczytany o 18:02:05 —
            # zawierał ciąg dalszy loga z komendy `pkg install
            # gradle` sprzed kilkunastu minut, nie nic z nowego
            # builda. Każda auto-backgroundowana komenda dostaje
            # teraz WŁASNY, unikalny plik logu.
            log = Path(
                "/data/data/com.termux/files/home/"
                "agent_background_"
                + datetime.now().strftime("%H%M%S_%f")
                + ".log"
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

        bg_result = {
            "ok": True,
            "pid": proc.pid,
            "command": command,
            "workdir": cwd,
            "log_file": str(log)
        }

        quoting_warning = _detect_single_quoted_shell_variable(command)

        if quoting_warning:
            bg_result["shell_quoting_warning"] = quoting_warning

        missing_file_warning = _detect_missing_cat_substitution_file(
            command
        )

        if missing_file_warning:
            bg_result["missing_file_warning"] = missing_file_warning

        return bg_result

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
        "test -e " + shlex.quote(path)
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
        "wc -c < " + shlex.quote(path)
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
        "unzip -l " + shlex.quote(path)
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


# ------------------------------------------------------------
# Narzędzie może zwrócić ok=true (samo WYWOŁANIE się powiodło —
# proces wystartował, plik dało się odczytać) mimo że TREŚĆ wyniku
# opisuje prawdziwą awarię (nieudany build, wyjątek, brakujący
# moduł). Zaobserwowany realny przypadek: `gradlew assembleDebug`
# skończył się "BUILD FAILED", ale samo polecenie shell zwróciło
# kod wyjścia z którego Gemini/MAIN nie zawsze wyciągali wniosek,
# że TO jest błąd — ok=true w wyniku narzędzia wygląda jak sukces
# na pierwszy rzut oka. To NIE jest specyficzne dla Gradle/gier —
# dotyczy każdego narzędzia zwracającego stdout/stderr/content
# dowolnego builda/skryptu/procesu, więc sprawdzane jest centralnie
# tutaj, dla WSZYSTKICH narzędzi na raz, zamiast osobno w każdym.
#
# Celowo tylko OSTRZEŻENIE (nie zmienia "ok"): fałszywy pozytyw
# (np. plik tekstowy opisujący jak naprawić błąd) nie powinien
# blokować prawdziwego sukcesu — ale Gemini/MAIN dostają jawny
# sygnał, żeby nie uznawać tego automatycznie za ukończone zadanie.
# ------------------------------------------------------------

_EMBEDDED_FAILURE_SIGNATURES = [
    "BUILD FAILED",
    "FAILURE: Build failed",
    "Traceback (most recent call last):",
    "npm ERR!",
    "FATAL EXCEPTION",
    "Segmentation fault",
    "core dumped",
    "INSTALL_FAILED_",
    "ModuleNotFoundError:",
    "cannot find symbol",
    "fatal error:",
    "SyntaxError:",
    "panic:",
    "Unhandled promise rejection",
    "ERR_MODULE_NOT_FOUND",
]


def _find_embedded_failure_signature(result):

    if not isinstance(result, dict):
        return None

    for key in ("stdout", "stderr", "content"):

        value = result.get(key)

        if not isinstance(value, str):
            continue

        lowered = value.lower()

        for signature in _EMBEDDED_FAILURE_SIGNATURES:
            if signature.lower() in lowered:
                return signature

    return None


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

            signature = _find_embedded_failure_signature(result)

            if signature:

                result["content_warning"] = (
                    "UWAGA: to narzędzie zwróciło ok=true, ale "
                    "treść wyniku zawiera fragment '" + signature
                    + "' — to WYGLĄDA jak prawdziwa awaria (nieudany "
                    "build, wyjątek, brakujący moduł) mimo że samo "
                    "wywołanie narzędzia się powiodło. NIE traktuj "
                    "tego automatycznie jako sukcesu — sprawdź "
                    "treść uważnie przed zgłoszeniem zadania jako "
                    "wykonanego."
                )

                log(
                    "GEMINI",
                    "Wykryto sygnaturę błędu w treści wyniku mimo "
                    "ok=true: '" + signature + "' (narzędzie: "
                    + name + ")"
                )

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
                args.get("content", ""),
                bool(args.get("append", False))
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

        # Gemini: android_long_click
        # Python: android_long_click_text
        if name == "android_long_click":

            fn = globals().get(
                "android_long_click_text"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_long_click_text()."
                }

            text = args.get("text")

            if text is None:
                return {
                    "ok": False,
                    "error":
                        "android_long_click wymaga text.",
                    "arguments": args
                }

            return fn(text)

        # Gemini: android_set_clipboard
        # Python: android_set_clipboard
        if name == "android_set_clipboard":

            fn = globals().get(
                "android_set_clipboard"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_set_clipboard()."
                }

            text = args.get("text")

            if text is None:
                return {
                    "ok": False,
                    "error":
                        "android_set_clipboard wymaga text.",
                    "arguments": args
                }

            return fn(text)

        # Gemini: android_paste_text
        # Python: android_paste_text
        if name == "android_paste_text":

            fn = globals().get(
                "android_paste_text"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_paste_text()."
                }

            text = args.get("text")
            target_text = args.get("target_text")

            if text is None or target_text is None:
                return {
                    "ok": False,
                    "error":
                        "android_paste_text wymaga text i "
                        "target_text.",
                    "arguments": args
                }

            return fn(text, target_text)

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

        # Gemini: android_screenshot_ocr
        # Python: android_screenshot_ocr
        if name == "android_screenshot_ocr":

            fn = globals().get(
                "android_screenshot_ocr"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_screenshot_ocr()."
                }

            return _call_tool_function(
                fn,
                args
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

        # Gemini: android_list_packages
        # Python: android_list_packages
        if name == "android_list_packages":

            fn = globals().get(
                "android_list_packages"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_list_packages()."
                }

            return fn(
                args.get("filter_text")
            )

        # Gemini: android_assert_text_visible
        # Python: android_assert_text_visible
        if name == "android_assert_text_visible":

            fn = globals().get(
                "android_assert_text_visible"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji "
                        "android_assert_text_visible()."
                }

            return fn(
                args.get("text")
            )

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

        if name == "chrome_execute_js":

            fn = globals().get(
                "chrome_execute_js"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji chrome_execute_js()."
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


# Zaobserwowany realny wzorzec (log 2026-08-28): Gemini uruchomił
# `ls -la plik.html && grep -iE "bland|retell|vapi|twilio" plik.html`
# przez termux_run. `ls` się powiódł, ale `grep` NIE znalazł dopasowania
# (plik istniał i był kompletny, po prostu nie zawierał tych słów
# dosłownie) — w Unixie to zwyczajowo kod wyjścia 1, NIE awaria. Kod
# niżej traktuje KAŻDY niezerowy kod wyjścia identycznie jak prawdziwy
# crash (`"ok": result.returncode == 0`) — to poprawne ogólne zachowanie
# (Python nie ma jak z góry wiedzieć, czy dany kod znaczy "nic nie
# znaleziono" czy "coś się zepsuło"), ale kosztowało to zespół 3 rundy
# konsultacji (~7 minut), zanim się domyślił z samego kontekstu. Ta
# heurystyka NIE zmienia interpretacji "ok"/błąd (zbyt ryzykowne —
# fałszywie ujemne dopasowanie ukryłoby prawdziwy błąd) — tylko dokleja
# PODPOWIEDŹ do raportu, gdy wzorzec pasuje do znanych narzędzi
# filtrujących (grep/diff/cmp/pgrep/test), których kod 1 zwyczajowo
# znaczy "brak dopasowania/warunek fałszywy", nie awarię.
_BENIGN_NONZERO_EXIT_COMMAND_RE = re.compile(
    r'(^|[|&;]|\s)(grep|egrep|fgrep|diff|cmp|pgrep|test)\s'
)


def _shell_exit_1_may_be_benign(tool_name, result):

    if tool_name not in ("termux_run", "termux_run_background"):
        return False

    if not isinstance(result, dict):
        return False

    if result.get("returncode") != 1:
        return False

    stderr = str(result.get("stderr") or "").strip()

    if stderr:
        return False

    command = str(result.get("command") or "")

    return bool(_BENIGN_NONZERO_EXIT_COMMAND_RE.search(command))


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

    # UWAGA: ten prompt jest wysyłany OD NOWA przy KAŻDYM TASK-u
    # (Interactions API nie ma tu odpowiednika trwałej sesji
    # DeepSeek/opendeep z system_prompt raz na start) — to
    # bezpośredni koszt tokenów Gemini na każde zadanie. Poniższa
    # treść jest celowo zwięzła (bez naddatku pustych linii/
    # powtórzeń), ale KAŻDA zasada niżej wynika z realnego,
    # zaobserwowanego incydentu (patrz komentarze w kodzie przy
    # poszczególnych narzędziach) — skracaj dalej ostrożnie, nie
    # usuwaj konkretów.
    #
    # ADAPTACYJNA DŁUGOŚĆ (2026-08-24, na wyraźną prośbę
    # użytkownika — "dynamiczne skracanie/rozszerzanie instrukcji
    # zależnie od sytuacji"): dwie najdłuższe reguły (interakcja z
    # ekranem Androida, monitorowanie procesu w tle) dotyczą wąskiej
    # klasy zadań — reszta zadań (Termux/pliki/shell) nie potrzebuje
    # ich pełnej, wielozdaniowej treści za każdym razem. Zamiast
    # usuwać te reguły, POKAZUJEMY PEŁNĄ WERSJĘ tylko gdy treść TASK-u
    #/warunku sukcesu faktycznie o tym mówi, a w pozostałych
    # przypadkach krótki, jednozdaniowy wskaźnik — pełne detale są
    # i tak zawsze dostępne w opisach poszczególnych narzędzi
    # (gemini_tools()), więc nic nie znika, tylko nie jest powtarzane
    # w każdej wiadomości bez potrzeby.
    _task_haystack = (
        str(task or "") + " " + str(success_condition or "")
    ).lower()

    _android_task_relevant = any(
        kw in _task_haystack for kw in (
            "android", "kalkulator", "zegar", "kalendarz",
            "aplikacj", "kliknij", "klikni", "wpisz", "przycisk",
            "ekran", "telefon", "chrome", "karta przegl", "otwórz",
            "otworz", "assert_text_visible", "android_click",
            "android_type", "android_tap", "android_press",
            "android_long_click", "android_paste_text", "wklej",
            "schowek",
        )
    )

    _background_task_relevant = any(
        kw in _task_haystack for kw in (
            "serwer", "w tle", "background", "proces", "monitoruj",
            "nasłuchuj", "nasluchuj", "port", "daemon",
        )
    )

    if _android_task_relevant:
        rule_6_block = """6. android_type wymaga, żeby na ekranie było FAKTYCZNIE skupione
   EDYTOWALNE pole tekstowe — działa dobrze w wyszukiwarkach,
   formularzach, notatkach. Wiele kalkulatorów i klawiatur
   numerycznych to zwykłe PRZYCISKI bez żadnego pola tekstowego —
   android_type może wtedy nie trafić nigdzie (wpisany tekst
   pojawia się na chwilę i znika, ekran zostaje pusty). Zaobserwowany
   realny przypadek: użytkownik patrzył na ekran na żywo — po
   android_type w kalkulatorze cyfry mignęły i zniknęły, kalkulator
   został pusty. Jeśli po android_type + krótkim odczekaniu
   android_assert_text_visible na WPISANYM tekście (nie na wyniku)
   zwraca found=false, albo od razu widzisz, że ekran ma same
   przyciski bez pola tekstowego — NIE próbuj android_type ponownie
   w kółko. Zamiast tego kliknij KAŻDY przycisk osobno przez
   android_click po jego tekście (np. "1", "5", "+", "2", "7", "=").
   Jeśli po android_type na ekranie pojawi się systemowe menu
   zaznaczania/udostępniania Androida (przycisk "Udostępnij",
   "Wyślij do urządzenia", Bluetooth, e-mail — zaobserwowany realny
   przypadek na żywym ekranie), to jednoznaczny sygnał, że tekst
   próbował się ZAZNACZYĆ zamiast wpisać: android_press('back')
   żeby to natychmiast zamknąć, NIE klikaj niczego w tym menu.

   To samo zaznaczanie daje DODATKOWĄ, alternatywną drogę wpisania
   tekstu zamiast klikania każdego przycisku osobno — zaobserwowany
   realny przypadek: użytkownik ręcznie przytrzymał pole na ekranie
   kalkulatora i zobaczył opcje "Wklej" i "Wybierz wszystko", czyli
   pole JEST selekcjonowalne mimo że nie przyjmuje zwykłego
   wpisywania. Jeśli klikanie pojedynczych przycisków jest
   niepraktyczne (długi tekst), użyj od razu android_paste_text(text,
   target_text) — to JEDNO wywołanie robi cały ciąg (ustaw schowek ->
   przytrzymaj -> kliknij "Wklej") i jasno mówi, na którym kroku coś
   ewentualnie zawiodło, zamiast ręcznego składania trzech osobnych
   narzędzi, gdzie łatwo pomylić kolejność albo kliknąć złą opcję z
   menu (np. "Udostępnij" zamiast "Wklej"). target_text to aktualnie
   widoczny tekst pola, które trzeba przytrzymać (np. "0" dla pustego
   wyświetlacza). Jeśli android_paste_text zwróci błąd — wróć do
   klikania pojedynczych przycisków.

   Niezależnie od metody: android_type/android_click same z siebie
   NIE zatwierdzają akcji — to jak wypełnienie formularza bez
   wciśnięcia Enter. Jeśli zadanie wymaga WYNIKU jakiejś akcji
   (obliczenie, wyszukiwanie, wysłanie formularza), po wpisaniu
   ZAWSZE wykonaj krok zatwierdzający właściwy dla tej aplikacji
   (android_press z KEYCODE_ENTER, kliknięcie "=" / przycisku
   wyszukiwania/wysyłania przez android_click) i dopiero POTEM
   sprawdzaj rezultat."""
    else:
        rule_6_block = """6. Ekran/aplikacje Android (gdyby jednak okazały się potrzebne w
   tym zadaniu): android_type działa tylko z prawdziwym, skupionym
   polem tekstowym, a samo wpisanie/kliknięcie NIE zatwierdza akcji
   (potrzeba Enter/"="/przycisku). Pełne szczegóły, znane pułapki i
   alternatywy (klikanie przycisków, android_paste_text) są opisane
   przy tych narzędziach — sięgnij po nie, jeśli faktycznie z nich
   korzystasz."""

    if _background_task_relevant:
        rule_7_block = """7. Monitorując proces w tle (termux_check_process /
   termux_read_file na log_file): max 2-3 sprawdzenia w TYM
   zadaniu. Jeśli nadal działa, zakończ raport z PID i ścieżką do
   log_file — NIE zapętlaj się aż do wyczerpania limitu narzędzi,
   MAIN utworzy kolejny TASK sprawdzający ten sam proces później."""
    else:
        rule_7_block = """7. Proces w tle (gdyby jednak był potrzebny): max 2-3 sprawdzenia
   w tym zadaniu, potem zostaw PID/log_file kolejnemu TASK-owi —
   nie zapętlaj się aż do wyczerpania limitu narzędzi."""

    prompt = f"""
Jesteś wykonawcą autonomicznego agenta. DeepSeek to mózg, Ty
wykonujesz REALNIE jego zadania dostępnymi narzędziami (Termux,
shell, zapis plików, Android/uiautomator2, Chrome/CDP jeśli
dostępny).

NIE pytaj użytkownika o zgodę. NIE kończ po samym zaplanowaniu.
NIE zgłaszaj sukcesu bez sprawdzenia rezultatu.

ZASADY:

1. Nowy/mały plik: termux_write_file. Fragment DUŻEGO już
   istniejącego pliku (np. poprawka błędu w kodzie gry):
   termux_patch_file (search/replace) — NIE przepisuj całego pliku
   przez termux_write_file, to marnuje tokeny i ryzykuje literówkę
   w części, której nie musiałeś dotykać (najpierw termux_read_file,
   żeby skopiować dokładny fragment do 'search'). NIGDY
   `sed -i 'N,Md'` ani inne usuwanie/zamiana PO NUMERZE LINII w
   plikach projektu (build.gradle, kod gry itp.) — realny przypadek:
   `sed -i '10,12d'` urwał środek bloku
   `allprojects {{ repositories {{ ... }} }}` i zepsuł build na kilka
   kolejnych kroków. Numery linii są kruche, termux_patch_file
   dopasowuje po TREŚCI i nie ma tego problemu.

2. Krótka komenda: termux_run. Długi proces: termux_run_background.

3. Po uruchomieniu programu/serwera/aplikacji: sprawdź, czy
   faktycznie działa — nie zgłaszaj sukcesu na słowo.

4. Błąd narzędzia: NIE NAPRAWIAJ GO SAM. Zatrzymaj TASK, zachowaj
   dokładny błąd, zwróć go do MAIN — to on decyduje o zmianie
   strategii, przygotowuje PATCH i następny TASK. Nie powtarzaj tej
   samej czynności po błędzie bez nowego TASK/PATCH od MAIN.

5. Po każdym udanym działaniu sprawdź faktyczny rezultat.

{rule_6_block}

{rule_7_block}

8. NIGDY nie zapisuj plików w /tmp — to katalog systemu Android,
   Termux (zwykła aplikacja) nie ma tam praw zapisu ("Permission
   denied"). Użyj $HOME (~) albo $PREFIX/tmp, np. "echo OK > ~/x.txt"
   zamiast "echo OK > /tmp/x.txt".

9. Nie wiesz jak kontynuować (typowo: nie wiesz jaki fragment podać
   jako 'search' w termux_patch_file)? Najpierw termux_read_file.
   Jeśli to nie wystarczy, ask_deepseek — krótka podpowiedź, potem
   kontynuujesz TEN SAM TASK. Limitowane do kilku razy na zadanie,
   nie zastępuj tym zwykłego czytania plików.

10. Usuwanie plików/katalogów: termux_delete, NIE `rm` przez
    shell/termux_run (obie wymagają potwierdzenia operatora, ale
    termux_delete pokazuje konkretną ścieżkę zamiast surowej
    komendy). Operator odmówił? Koniec zadania, zgłoś odmowę do
    MAIN — nie próbuj obejść tego inną komendą ani sformułowaniem.

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

    # Sygnały z narzędzi (content_warning z v29, compile_error z v43,
    # blocked_fake_tool_invocation z v39 itp.) widzi na bieżąco tylko
    # Gemini w trakcie TEGO zadania — jeśli samo ich nie wspomni w
    # swoim dowolnym, tekstowym podsumowaniu (a wiemy z realnych
    # logów, że potrafi konfabulować/pomijać), MAIN/PLANNER/CRITIC
    # nigdy się o nich nie dowiedzą. Zbieramy je tu NIEZALEŻNIE od
    # tego, co Gemini napisze, i dołączamy do zwracanego wyniku
    # zadania — to gwarantuje, że zespół dostanie te sygnały
    # strukturalnie, a nie tylko "jeśli Gemini raczy wspomnieć".
    # Zdefiniowane PRZED try:, żeby było dostępne nawet gdy wyjątek
    # wystąpi już przy samym client.interactions.create(...)
    # (np. ścieżka QUOTA_EXHAUSTED).
    collected_warnings = []

    # PEŁNA lista wywołanych narzędzi w TYM zadaniu (nazwa + ok) —
    # wcześniej zapisywana była tylko ICH LICZBA (tool_calls), więc
    # nie dało się sprawdzić, CZY konkretne narzędzie weryfikujące
    # faktycznie zostało użyte, zanim raport napisał "potwierdzone".
    # To jest ta "większa zmiana strukturalna", o którą poprosił
    # użytkownik — checklist (patrz _checklist_record_result) używa
    # tego teraz do odróżnienia realnie sprawdzonego faktu od samej
    # deklaracji Gemini w tekście raportu.
    collected_tool_trace = []

    # Konkretne fragmenty tekstu, które android_assert_text_visible
    # NIEZALEŻNIE potwierdził jako widoczne na ekranie w TRAKCIE tego
    # zadania (found=True) — to jedyny rodzaj dowodu stanu urządzenia,
    # jaki uznajemy za "zweryfikowany", bo to jednoznaczny wynik
    # narzędzia, nie interpretacja dużego zrzutu przez Gemini.
    collected_confirmed_texts = []

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
                    "interaction_id": interaction_id,
                    "tool_warnings": collected_warnings,
                    "tool_trace": collected_tool_trace,
                    "confirmed_texts": collected_confirmed_texts
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

                if isinstance(result, dict):

                    for warning_key in (
                        "content_warning",
                        "compile_error",
                        "blocked_fake_tool_invocation",
                        "custom_tool_rejected",
                        "already_launched_note",
                        "shell_quoting_warning",
                        "missing_file_warning",
                        "stale_warning"
                    ):

                        if result.get(warning_key):

                            collected_warnings.append(
                                name + " [" + warning_key + "]: "
                                + short(
                                    str(result[warning_key]),
                                    300
                                )
                            )

                collected_tool_trace.append({
                    "tool": name,
                    "ok": (
                        result.get("ok")
                        if isinstance(result, dict) else None
                    )
                })

                # Patrz komentarz przy _decision_asks_for_contact_info()
                # (przed _handle_need_user_login) — zapamiętujemy, że
                # agent w tej sesji faktycznie SPRÓBOWAŁ poszukać
                # kontaktu na telefonie, niezależnie od tego, czy się
                # udało (nieudana próba to wciąż informacja, że
                # sprawdzono).
                if (
                    name in ("termux_run", "termux_run_background")
                    and "termux-contact-list" in str(
                        args.get("command", "")
                        if isinstance(args, dict) else ""
                    )
                ):
                    _mark_contacts_lookup_attempted()

                if (
                    name == "android_assert_text_visible"
                    and isinstance(result, dict)
                    and result.get("ok") is True
                    and result.get("found") is True
                ):
                    confirmed_text = str(
                        result.get("text", "")
                    ).strip()

                    if len(confirmed_text) >= 2:
                        collected_confirmed_texts.append(
                            confirmed_text
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

                    benign_exit_hint = (
                        " UWAGA (automatyczna heurystyka): ta komenda "
                        "zawiera grep/egrep/fgrep/diff/cmp/pgrep/test, "
                        "kod wyjścia to dokładnie 1, a stderr jest "
                        "PUSTE — to zwyczajowo znaczy 'nie znaleziono "
                        "dopasowania / warunek fałszywy', NIE że "
                        "narzędzie się zepsuło. Jeśli reszta polecenia "
                        "wykonała się poprawnie, rozważ czy to nie jest "
                        "po prostu brak wyniku wyszukiwania (np. inny "
                        "wzorzec, treść ładowana przez JS), zanim "
                        "uznasz to za prawdziwą awarię wymagającą "
                        "innego podejścia."
                        if _shell_exit_1_may_be_benign(name, result)
                        else ""
                    )

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
                            + benign_exit_hint
                        ),
                        "interaction_id": interaction_id,
                        "tool_warnings": collected_warnings,
                        "tool_trace": collected_tool_trace,
                        "confirmed_texts": collected_confirmed_texts
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
                    "tool_calls": tool_calls,
                    "tool_warnings": collected_warnings,
                    "tool_trace": collected_tool_trace,
                    "confirmed_texts": collected_confirmed_texts
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
            "interaction_id": interaction_id,
            "tool_warnings": collected_warnings,
            "tool_trace": collected_tool_trace,
            "confirmed_texts": collected_confirmed_texts
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
                ),
                "tool_warnings": collected_warnings,
                "tool_trace": collected_tool_trace,
                "confirmed_texts": collected_confirmed_texts
            }

        return {
            "ok": False,
            "status": "GEMINI_EXECUTOR_ERROR",
            "key": key_name,
            "error": short(
                error_text,
                3000
            ),
            "tool_warnings": collected_warnings,
            "tool_trace": collected_tool_trace,
            "confirmed_texts": collected_confirmed_texts
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


# ============================================================
# CHECKLIST PUNKTÓW CELU (progress_checklist.json)
# ============================================================
#
# Zaobserwowany realny problem (log z 2026-08-23): Gemini w swoim
# raporcie NAPISAŁ "Wynik dzialania 12+7 = 19 potwierdzony przez
# android_state" i to zdanie trafiło do pliku przez zwykłe echo —
# to była DEKLARACJA, nie dowód, nikt tego nie sprawdził niezależnie.
# Kilka kroków później PROGRESS_ESTIMATOR (inna rola DeepSeek) sam
# to zauważył: "brak fizycznych dowodów". Ten moduł ma dać zespołowi
# ZWIĘZŁY, zawsze aktualny rejestr KTÓRE punkty (TASK-i) są faktycznie
# zweryfikowane przez Python (nie tylko zadeklarowane), i strukturalnie
# (nie tylko przez prośbę w prompcie) zablokować zlecenie punktu,
# który już ma potwierdzony dowód — zamiast pozwalać zespołowi w kółko
# wracać do tego samego.
#
# Weryfikacja dowodu jest CELOWO ograniczona do plików — to jedyny
# fakt, który Python może sam, niezależnie od LLM, sprawdzić na dysku
# (ten sam mechanizm co _extract_goal_mentioned_files/verify_final).
# Nie próbujemy parsować dowolnej wartości z android_state/chrome —
# to by wymagało zgadywania formatu i byłoby równie kruche jak to,
# co ma zastąpić.

def _normalize_task_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _load_progress_checklist():
    data = read_json(PROGRESS_CHECKLIST_FILE, [])
    if not isinstance(data, list):
        return []
    return data


def _save_progress_checklist(items):
    write_json(PROGRESS_CHECKLIST_FILE, items)


def _checklist_add(task_id, task, success_condition):
    items = _load_progress_checklist()
    items.append({
        "task_id": task_id,
        "task": task,
        "success_condition": success_condition,
        "status": "W_TOKU",
        "created": datetime.now().isoformat(),
        "evidence": "",
        "tool_trace": []
    })
    _save_progress_checklist(items)


def _verify_success_condition_evidence(success_condition, confirmed_texts=None):
    """
    Próbuje NIEZALEŻNIE potwierdzić warunek sukcesu, w kolejności:

    1. Plik wprost wymieniony w warunku (ten sam wzorzec ścieżek co
       _extract_goal_mentioned_files) — sprawdzany BEZPOŚREDNIO na
       dysku przez Python, niezależnie od tego, co Gemini deklaruje.

    2. Tekst na ekranie — TYLKO jeśli w TRAKCIE TEGO SAMEGO zadania
       narzędzie android_assert_text_visible faktycznie zwróciło
       found=True dla fragmentu, który pojawia się też w treści
       warunku sukcesu (confirmed_texts, patrz gemini_execute_task).
       To jest sedno tej poprawki: wcześniej dowolna deklaracja typu
       "wynik 19 potwierdzony przez android_state" w raporcie Gemini
       była nie do odróżnienia od realnego sprawdzenia — teraz liczy
       się WYŁĄCZNIE jednoznaczny wynik narzędzia weryfikującego,
       faktycznie wywołanego w tym zadaniu, nie proza raportu.

    Zwraca (True, opis_dowodu) albo (False, "") gdy nie da się tego
    sprawdzić bez ufania samej deklaracji Gemini.
    """
    for rel_path in _extract_goal_mentioned_files(success_condition):
        try:
            p = Path(rel_path).expanduser()
        except Exception:
            continue
        if p.exists() and p.stat().st_size > 0:
            return True, "plik " + rel_path + " istnieje i nie jest pusty"

    condition_lower = str(success_condition or "").lower()

    for confirmed_text in (confirmed_texts or []):
        needle = str(confirmed_text).strip()
        if len(needle) >= 2 and needle.lower() in condition_lower:
            return True, (
                "android_assert_text_visible potwierdził tekst '"
                + needle + "' faktycznie widoczny na ekranie"
            )

    return False, ""


def _checklist_record_result(task_id, result):
    if not isinstance(result, dict):
        return

    items = _load_progress_checklist()
    changed = False

    for item in items:

        if item.get("task_id") != task_id:
            continue

        changed = True

        item["tool_trace"] = result.get("tool_trace", [])

        if result.get("status") == "COMPLETED":
            verified, evidence = _verify_success_condition_evidence(
                item.get("success_condition", ""),
                result.get("confirmed_texts", [])
            )
            if verified:
                item["status"] = "ZWERYFIKOWANY"
                item["evidence"] = evidence
            else:
                item["status"] = "ZADEKLAROWANY_BEZ_DOWODU"
                item["evidence"] = ""
        else:
            item["status"] = "BLAD"

        item["finished"] = datetime.now().isoformat()
        break

    if changed:
        _save_progress_checklist(items)


def _success_condition_already_satisfied_message(task_text, success_condition):
    """
    Zaobserwowany realny problem (log 2026-08-24, ~10 minut, 6
    kolejnych krokow): MAIN wielokrotnie tworzyl NOWE TASKi typu
    "zweryfikuj czy plik X istnieje" dla pliku, ktory Python moze
    sprawdzic SAM i NATYCHMIAST, bez angazowania w ogole Gemini ani
    zespolu DeepSeek — a mimo to caly zespol (PLANNER/CRITIC/MAIN/
    Gemini) w kolko przechodzil przez pelny cykl konsultacji tylko
    po to, zeby ponownie potwierdzic to, co bylo prawda juz wczesniej.
    Uzytkownik trafnie to nazwal: system "za bardzo skupia sie na
    dowodach, nie rozwiazywaniu problemu".

    Jesli WSZYSTKIE pliki wymienione w warunku sukcesu NOWEGO taska
    (rozpoznawane tym samym wzorcem co _verify_success_condition_
    evidence) juz istnieja i nie sa puste W TEJ CHWILI, nie ma sensu
    tworzyc taska ani angazowac Gemini — Python juz zna odpowiedz.
    Od razu zapisuje ten punkt jako ZWERYFIKOWANY w checkliscie (co
    dodatkowo pozwoli _checklist_duplicate_message zlapac identyczne
    powtorzenia w przyszlosci) i zwraca gotowy komunikat zamiast
    None. Zwraca None, gdy trzeba faktycznie cos wykonac (brak
    wymienionych plikow, albo ktorys jeszcze nie istnieje/jest pusty).

    Zaobserwowany realny problem (log 2026-08-25, "uniwersalny2",
    KROK 7-10): cel budował JEDEN wspólny plik (test_uniwersalny2.txt)
    przez WIELE różnych punktów (1, 6, 7, i próbę 4). Skoro punkty
    1/6/7 już go zapisały, plik istniał i był niepusty — więc TA
    funkcja krótko spinała KAŻDY kolejny TASK wspominający ten sam
    plik jako "już spełniony", W TYM punkt 4 (użycie narzędzia
    potęgowania), który w ogóle jeszcze nie był zrobiony. Sama
    obecność pliku nic nie mówi o tym, czy zawiera TREŚĆ potrzebną
    akurat TEMU zadaniu — a ta funkcja tego nie ocenia (nie ma jak,
    bez kolejnego modelu). Efekt: zespół nie mógł w ogóle zlecić
    Gemini uzupełnienia punktu 4, bo Python sam, z góry, uznawał to
    za zbędne — 4 kroki zmarnowane na obchodzenie własnej blokady.
    Naprawione: jeśli ten sam plik jest już wymieniony w INNYM
    (o innej treści task) zweryfikowanym punkcie checklisty — czyli
    to potwierdzony, współdzielony/rosnący plik — nie stosujemy tego
    skrótu; TASK ma faktycznie się wykonać i zostać zweryfikowany
    normalnie po fakcie, nie z góry odrzucony na podstawie samej
    obecności pliku.
    """

    files = _extract_goal_mentioned_files(success_condition)

    if not files:
        return None

    normalized_task = _normalize_task_text(task_text)
    checklist_items = _load_progress_checklist()

    evidence_parts = []

    for rel_path in files:
        try:
            p = Path(rel_path).expanduser()
        except Exception:
            return None
        if not (p.exists() and p.stat().st_size > 0):
            return None

        for item in checklist_items:

            if item.get("status") != "ZWERYFIKOWANY":
                continue

            if _normalize_task_text(item.get("task", "")) == normalized_task:
                continue

            if rel_path in _extract_goal_mentioned_files(
                item.get("success_condition", "")
            ):
                # Plik współdzielony przez inny, już zweryfikowany
                # (ale INNY) punkt — sama jego obecność nie dowodzi
                # niczego o TYM zadaniu. Niech się faktycznie wykona.
                return None

        evidence_parts.append(
            rel_path + " (" + str(p.stat().st_size) + " B)"
        )

    evidence = "już na dysku: " + ", ".join(evidence_parts)

    items = _load_progress_checklist()
    items.append({
        "task_id": (
            "auto_precheck_"
            + datetime.now().strftime("%Y%m%d%H%M%S%f")
        ),
        "task": task_text,
        "success_condition": success_condition,
        "status": "ZWERYFIKOWANY",
        "created": datetime.now().isoformat(),
        "finished": datetime.now().isoformat(),
        "evidence": evidence,
        "tool_trace": []
    })
    _save_progress_checklist(items)

    return (
        "Wszystkie pliki wymienione w warunku sukcesu JUŻ ISTNIEJĄ "
        "i nie są puste W TEJ CHWILI (sprawdzone bezpośrednio na "
        "dysku, bez angażowania Gemini): " + evidence + ". Ten "
        "punkt jest już faktycznie spełniony — zaproponuj NASTĘPNY, "
        "faktycznie jeszcze niezrobiony krok zamiast tworzenia "
        "taska tylko po to, żeby ponownie to potwierdzić."
    )


def _checklist_duplicate_message(task_text):
    """
    Zwraca gotowy komunikat blokady, jeżeli task_text to dokładne
    powtórzenie punktu już ZWERYFIKOWANEGO dowodem z dysku — albo
    None, gdy nie ma kolizji. Celowo TYLKO dokładne dopasowanie (po
    normalizacji białych znaków/wielkości liter), nie fuzzy-matching
    — żeby nie zablokować przez pomyłkę faktycznie innego zadania.
    """
    normalized = _normalize_task_text(task_text)

    if not normalized:
        return None

    for item in _load_progress_checklist():

        if item.get("status") != "ZWERYFIKOWANY":
            continue

        if _normalize_task_text(item.get("task", "")) == normalized:
            return (
                "Ten TASK jest IDENTYCZNY z punktem już "
                "ZWERYFIKOWANYM dowodem z dysku (task_id="
                + str(item.get("task_id", "?")) + ", dowód: "
                + str(item.get("evidence", "")) + "). Nie powtarzaj "
                "go — zaproponuj KOLEJNY, inny krok."
            )

    return None


_CHECKLIST_STATUS_LABELS = {
    "ZWERYFIKOWANY": "zweryfikowany dowodem",
    "ZADEKLAROWANY_BEZ_DOWODU": "zadeklarowany przez Gemini, BEZ dowodu",
    "BLAD": "zakończony błędem",
    "W_TOKU": "w toku"
}


def _checklist_refresh_failed_items():
    """
    Promuje punkty ze statusem BLAD do ZWERYFIKOWANY, jeżeli ich
    warunek sukcesu jest TERAZ faktycznie spełniony na dysku —
    niezależnie od tego, który TASK go ostatecznie spełnił.

    ZAOBSERWOWANY REALNY, KOSZTOWNY BUG (log 2026-08-28, cel "zadzwoń
    do Beaty", KROKI 6-13): `_checklist_record_result()` ustawia
    "BLAD" wyłącznie dla pozycji o TYM SAMYM task_id, a ponowna próba
    tej samej rzeczy dostaje NOWY task_id — czyli NOWĄ pozycję na
    liście. Nic w całym pliku nigdy nie zdejmowało starego "BLAD"
    (sprawdzone: `item["status"] = "BLAD"` występuje w dokładnie
    jednym miejscu, bez żadnego odpowiednika cofającego), mimo że
    docstring _checklist_summary_block() wprost obiecywał, że błędy
    znikają, "dopóki się nie pojawi ich naprawiony odpowiednik" —
    ta obietnica NIGDY nie została zaimplementowana.

    Skutek w realnym logu: dwa skrypty padły na SyntaxError (KROK 4 i
    6), po czym w KROKU 8 Gemini wykonało DOKŁADNIE tę samą pracę
    poprawnie (pobrało stronę, znalazło linki, zapisało
    api_docs_url.txt) — a mimo to CRITIC w krokach 6, 7, 8, 9, 11 i 13
    blokował KAŻDY kolejny plan, cytując "3 aktywne błędy w STANIE
    FAKTYCZNYM, które wymagają ponowienia". Zespół nie miał ŻADNEJ
    drogi wyjścia: te pozycje były strukturalnie nie do zamknięcia.
    W KROKU 9 MAIN, pod presją tej blokady, zlecił zadanie-atrapę
    (zapis pliku "resolved_script_errors.txt" z tekstem, że błędy
    zostały rozwiązane) — co oczywiście niczego nie zmieniło i CRITIC
    blokował dalej. Co najmniej 5 pełnych tur zespołu zmarnowanych na
    martwą blokadę.

    Weryfikacja używa DOKŁADNIE tego samego, deterministycznego
    sprawdzenia na dysku co reszta checklisty
    (_verify_success_condition_evidence) — więc nie wprowadza nowego
    "miękkiego" kryterium ani nowej powierzchni do oszukania: jeżeli
    warunek sukcesu nie wymienia pliku, którego istnienie da się
    sprawdzić, pozycja zostaje BLAD (zachowawczo).
    """

    items = _load_progress_checklist()
    changed = False

    for item in items:

        if item.get("status") != "BLAD":
            continue

        verified, evidence = _verify_success_condition_evidence(
            item.get("success_condition", "")
        )

        if verified:
            item["status"] = "ZWERYFIKOWANY"
            item["evidence"] = (
                evidence
                + " (warunek spełniony PÓŹNIEJ, innym podejściem niż "
                "to, które pierwotnie zawiodło)"
            )
            changed = True

    if changed:
        _save_progress_checklist(items)

    return items


# Host z adresu URL — jedyny w pełni jednoznaczny sygnał "o jakiej
# zewnętrznej usłudze mowa", jaki da się wyciągnąć bez LLM-a. W
# realnych logach KAŻDE podejście miało swój adres (console.twilio.com,
# app.bland.ai, docs.vapi.ai, ainora.lt), więc pokrycie jest pełne.
_APPROACH_URL_PATTERN = re.compile(
    r"https?://([A-Za-z0-9.\-]+)",
    re.IGNORECASE
)

# Hosty, które są infrastrukturą TEGO agenta albo ogólnym internetem —
# nigdy nie są "podejściem do celu" i tylko zaśmiecałyby rejestr.
_APPROACH_IGNORED_HOSTS = {
    "127.0.0.1", "localhost", "0.0.0.0",
    "chat.deepseek.com", "deepseek.com",
    "google.com", "www.google.com",
    "github.com", "www.github.com",
    "developer.android.com",
    "example.com", "www.example.com",
}


def _approach_names_in(text):
    """
    Zwraca znormalizowane nazwy usług (np. "twilio", "bland",
    "ainora") wymienionych przez adres URL w podanym tekście.

    Normalizacja: bierzemy przedostatni człon hosta, czyli tę część,
    która faktycznie identyfikuje usługę niezależnie od subdomeny —
    console.twilio.com, api.twilio.com i www.twilio.com to jedno i to
    samo podejście, a nie trzy różne.
    """

    found = []

    for host in _APPROACH_URL_PATTERN.findall(str(text or "")):

        host = host.lower().strip(".")

        if host in _APPROACH_IGNORED_HOSTS:
            continue

        parts = [p for p in host.split(".") if p]

        if len(parts) < 2:
            continue

        name = parts[-2]

        if name in ("com", "co", "org", "net"):
            # np. "example.co.uk" — cofamy się o jeden człon dalej
            if len(parts) < 3:
                continue
            name = parts[-3]

        if name and name not in found:
            found.append(name)

    return found


def _load_approaches():
    data = read_json(APPROACHES_FILE, {})
    return data if isinstance(data, dict) else {}


def _approaches_record(task_text, success_condition, result):
    """
    Zapisuje, jak skończyło się podejście do usług wymienionych w
    tym zadaniu. Wołane po KAŻDYM wykonanym TASK-u.

    Świadomie nie ocenia "czy porzucić" — tylko rzetelnie notuje, ile
    razy próbowano i czym się skończyła ostatnia próba. Wniosek
    ("to już nie działa, spróbujmy inaczej") wyciąga zespół, mając
    wreszcie fakty przed oczami zamiast polegać na pamięci.
    """

    if not isinstance(result, dict):
        return

    haystack = " ".join([
        str(task_text or ""),
        str(success_condition or ""),
        json.dumps(result.get("arguments", {}), ensure_ascii=False)
        if isinstance(result.get("arguments"), dict) else "",
        str(result.get("report", "")),
    ])

    names = _approach_names_in(haystack)

    if not names:
        return

    failed = (
        result.get("ok") is False
        or result.get("status") == "GEMINI_TOOL_ERROR"
    )

    outcome = (
        str(
            result.get("error")
            or (result.get("tool_result") or {}).get("stderr")
            or result.get("message")
            or "błąd"
        )
        if failed
        else "OK"
    )

    data = _load_approaches()

    for name in names:

        entry = data.get(name) or {
            "kroki": 0,
            "bledy": 0,
            "ostatnio": ""
        }

        entry["kroki"] = int(entry.get("kroki", 0)) + 1

        if failed:
            entry["bledy"] = int(entry.get("bledy", 0)) + 1

        entry["ostatnio"] = short(outcome.replace("\n", " "), 160)
        data[name] = entry

    write_json(APPROACHES_FILE, data)


def _approaches_summary_block():
    """
    Zwięzła pamięć zespołu o tym, czego już próbowano — wstrzykiwana
    w kontekst każdej roli. Bez tego zespół co kilka kroków wracał do
    usługi, którą sam wcześniej odrzucił (patrz APPROACHES_FILE).
    """

    data = _load_approaches()

    if not data:
        return ""

    # Najpierw te, przy których było najwięcej roboty — to one
    # najczęściej wracały w kółko w realnych logach.
    ordered = sorted(
        data.items(),
        key=lambda kv: -int(kv[1].get("kroki", 0))
    )[:6]

    lines = [
        "CZEGO JUŻ PRÓBOWALIŚMY (rejestr Pythona — zanim "
        "zaproponujesz usługę z tej listy, sprawdź, czym skończyła "
        "się poprzednia próba):"
    ]

    for name, entry in ordered:
        lines.append(
            "- " + name
            + ": kroków " + str(entry.get("kroki", 0))
            + ", błędów " + str(entry.get("bledy", 0))
            + ", ostatnio: " + str(entry.get("ostatnio", "?"))
        )

    return "\n".join(lines)


def _checklist_summary_block():
    """
    Zwięzłe podsumowanie checklisty do wstrzyknięcia w kontekst
    zespołu — zero wywołań LLM, ten sam wzorzec co
    _goal_progress_snapshot(). To jest odpowiedź na "ile punktów
    zrobione" bez wysyłania pełnych logów: liczba + kilka ostatnich
    pozycji z jawną etykietą, czy to dowód czy tylko deklaracja.

    Zaobserwowany realny problem (log z 2026-08-23, 16:40-17:07):
    krok "kalkulator" zawiódł (BLAD) na 2. TASKu z ~15, ale skoro
    wcześniejsza wersja pokazywała tylko items[-5:] (5 NAJNOWSZYCH
    wpisów), ten porzucony punkt wypadł z widoku po kilku kolejnych
    TASKach — MAIN/PLANNER przestali go w ogóle widzieć w kontekście
    i przez pozostałe ~40 minut sesji nigdy do niego nie wrócili,
    zamiast tego zespół po prostu szedł dalej do nowych punktów.
    Błędy (BLAD) są więc TERAZ pokazywane ZAWSZE, niezależnie od
    tego, jak dawno powstały — dopóki się nie pojawi ich naprawiony
    odpowiednik. Za tę drugą część odpowiada
    _checklist_refresh_failed_items() wołane niżej (patrz jego
    docstring — przez wiele wersji ta obietnica NIE była w ogóle
    zaimplementowana, co zapętlało CRITIC-a na martwych błędach).
    """
    items = _checklist_refresh_failed_items()

    if not items:
        return ""

    verified = sum(1 for i in items if i.get("status") == "ZWERYFIKOWANY")
    unverified = sum(1 for i in items if i.get("status") == "ZADEKLAROWANY_BEZ_DOWODU")
    failed_items = [i for i in items if i.get("status") == "BLAD"]
    running = sum(1 for i in items if i.get("status") == "W_TOKU")

    lines = [
        "PUNKTY ZADAŃ (" + str(len(items)) + " łącznie): "
        + str(verified) + " zweryfikowanych dowodem, "
        + str(unverified) + " zadeklarowanych BEZ dowodu, "
        + str(len(failed_items)) + " błędów, " + str(running) + " w toku."
    ]

    if failed_items:
        lines.append(
            "⚠️ NIEDOKOŃCZONE (błąd, WYMAGAJĄ PONOWIENIA — nie "
            "porzucaj ich na rzecz nowych punktów):"
        )
        for item in failed_items[-5:]:
            lines.append("- " + short(item.get("task", ""), 100))

    recent_other = [
        i for i in items[-5:] if i.get("status") != "BLAD"
    ]

    if recent_other:
        lines.append("Ostatnie inne punkty:")
        for item in recent_other:
            label = _CHECKLIST_STATUS_LABELS.get(
                item.get("status"), str(item.get("status", "?"))
            )
            lines.append("- [" + label + "] " + short(item.get("task", ""), 100))

    return "\n".join(lines)


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

    _checklist_add(task_id, task, success_condition)

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

    _checklist_record_result(task["task_id"], result)

    # Pamięć o PODEJŚCIACH (jakie usługi już próbowano i jak się
    # to skończyło) — patrz APPROACHES_FILE.
    _approaches_record(
        task.get("task", ""),
        task.get("success_condition", ""),
        result
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

        generic_streak = record_generic_tool_failure_streak(
            result.get("tool")
        )

        result["generic_failure_streak"] = generic_streak

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

            if generic_streak >= GENERIC_TOOL_FAILURE_STREAK_LIMIT:

                result["hint"] += (
                    " DODATKOWO: narzędzie '" + str(result.get("tool"))
                    + "' zawiodło już " + str(generic_streak) + "x z "
                    "rzędu w tym celu — za każdym razem z INNYMI "
                    "argumentami (dlatego to NIE jest jeszcze "
                    "automatyczna eskalacja do CODE_REVIEWERA powyżej). "
                    "Jeśli to zadanie wymaga faktycznej logiki "
                    "(parsowanie, dopasowywanie danych, obsługa "
                    "wariantów), rozważ napisanie NARZĘDZIA w "
                    "custom_tools/ zamiast kolejnej pojedynczej "
                    "komendy powłoki — patrz kontrakt custom_tools w "
                    "promptcie ENGINEER."
                )

    elif result.get("ok"):

        task["status"] = "EXECUTED"

        reset_generic_tool_failure_streak()

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

    ENGINEER_PROMPT każe zwracać gotowy kod/polecenie zawsze
    wewnątrz dokładnie takiego bloku — ta
    funkcja pozwala Pythonowi wyciąć ten kod i zapisać go
    bezpośrednio do pliku (patrz write_engineer_code_to w
    run_agent()), zamiast zmuszać Gemini do przepisywania go od
    nowa z opisu słownego przygotowanego przez MAIN. Oszczędza to
    tokeny/limit Gemini i eliminuje błędy przepisywania — kod trafia
    do pliku 1:1 taki, jaki wymyślił DeepSeek.

    Ignoruje opcjonalny znacznik języka po otwierających ``` (np.
    ```python, ```java).

    Zaobserwowany realny przypadek: DeepSeek czasem zostawia
    pojedynczą spację/tabulator zaraz po otwierającym ``` i
    znaczniku języka, zanim zacznie się właściwy kod (np.
    "```python\n try:\n"). Zapisane 1:1 do pliku, to psuje
    wcięcie pierwszej linii (IndentationError: unexpected
    indent) — dlatego wiodące spacje/taby PRZED pierwszym
    faktycznym znakiem kodu są tu usuwane. Nie dotyka to wcięć
    W ŚRODKU kodu (tylko sam początek bloku).
    """

    match = re.search(
        r"```[a-zA-Z0-9_+-]*\n(.*?)```",
        text or "",
        re.DOTALL
    )

    if not match:
        return None

    code = match.group(1).lstrip(" \t").rstrip("\n")

    return code if code.strip() else None


_SHELL_SCRIPT_MARKERS = re.compile(
    r"^#!/|<<\s*['\"]?EOF['\"]?\s*$|^\s*cat\s+>>?\s|\$\(",
    re.MULTILINE
)


def _looks_like_shell_script(code, target_path):
    """
    Wykrywa, czy blok kodu wygląda jak SKRYPT POWŁOKI (komendy do
    URUCHOMIENIA), a nie treść pliku do zapisania 1:1.

    Zaobserwowany realny przypadek: ENGINEER podał w
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


_PYTHON_SCRIPT_MARKERS = re.compile(
    r"^\s*import\s+\S+\s*$|^\s*from\s+\S+\s+import\b|^\s*def\s+\w+\s*\(",
    re.MULTILINE
)


def _looks_like_python_script(code, target_path):
    """
    Lustrzane odbicie _looks_like_shell_script(): wykrywa, czy blok
    kodu wygląda jak PYTHON, a docelowa ścieżka to .sh — czyli
    zostanie zapisany 1:1, a potem uruchomiony przez `bash`, co dla
    prawdziwego kodu Pythona kończy się wyłącznie błędami składni.

    Zaobserwowany realny przypadek: ENGINEER podał kod zaczynający
    się od `import android` / `droid = android.Android()` (styl
    Android Scripting Layer), write_engineer_code_to zapisało to
    1:1 do check_voice_call_output.sh, a TASK uruchomił je przez
    `bash ~/check_voice_call_output.sh` — "import: command not
    found" na każdej linii z importem, "syntax error" na pierwszym
    wywołaniu funkcji ze składnią Pythona. Cały krok (konsultacja
    zespołu + TASK) poszedł na marne, mimo że niezgodność była
    wykrywalna od razu, samą treścią bloku kodu.
    """

    suffix = Path(str(target_path)).suffix.lower()

    if suffix != ".sh":
        return False

    return bool(_PYTHON_SCRIPT_MARKERS.search(code or ""))


def _python_syntax_error(code):
    """
    Zwraca czytelny opis błędu składni, jeśli `code` NIE jest
    poprawnym Pythonem — albo None, gdy jest poprawny.

    Zaobserwowany realny, DWUKROTNY przypadek w JEDNEJ sesji
    (2026-08-28, "fetch_ainora_api_docs.py", potem
    "find_ainora_docs.py"): ENGINEER podał w bloku kodu de facto
    POLECENIE URUCHOMIENIA ("cd ~/agent && pip install ... &&
    python plik.py", "python ~/agent/plik.py"), nie treść samego
    pliku Pythona. _looks_like_shell_script() tego NIE złapał — jego
    regex szuka konkretnych wzorców (shebang, heredoc, `cat >`,
    `$(...)`), a żadna z tych dwóch komend żadnego z nich nie
    zawiera. write_engineer_code_to zapisało to 1:1 jako "kod",
    Gemini uruchomił `python plik.py` i dostał natychmiastowy
    SyntaxError — cały krok (konsultacja zespołu + TASK) poszedł na
    marne, DWA RAZY, mimo że błąd był wykrywalny deterministycznie
    bez zgadywania kolejnych wzorców tekstowych: wystarczyło
    faktycznie spróbować SKOMPILOWAĆ to jako Python, dokładnie tak,
    jak zaraz zrobi to sam interpreter przy uruchomieniu.
    """

    try:
        compile(code, "<engineer_code>", "exec")
    except SyntaxError as e:
        return str(e)

    return None


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
# RESEARCHER WEB SEARCH / WEB FETCH
# ============================================================

# Kilka bloków HTML, których i tak nikt nie chce czytać jako
# "treść strony" — usuwane W CAŁOŚCI (razem z zawartością) przed
# ściągnięciem tagów, żeby np. kod JS nie wleciał w wynikowy tekst.
_HTML_STRIP_BLOCK_TAGS = re.compile(
    r"(?is)<(script|style|noscript)[^>]*>.*?</\1>"
)
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_HTML_ANY_TAG = re.compile(r"(?s)<[^>]+>")
_HTML_TITLE_TAG = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_HTML_CHARSET = re.compile(r"charset=([\w-]+)", re.IGNORECASE)


def _fetch_url_text(url, max_bytes=400000, max_chars=4000):
    """
    Pobiera stronę www BEZPOŚREDNIO przez Python (urllib, moduł
    standardowy — bez dodatkowych zależności), bez pośrednictwa
    Gemini/Chrome/telefonu. To osobna droga od chrome_tabs/
    chrome_inspect (które czytają kartę OTWARTĄ w Chrome na
    telefonie) — tu RESEARCHER prosi o URL, którego nikt nie musi
    mieć otwartego na ekranie.

    Zwraca zwykły tekst (tagi HTML odarte, encje odkodowane), nie
    surowy HTML — DeepSeek nie musi parsować znaczników.
    """

    url = str(url or "").strip().strip("<>\"'")

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AEL-MINI-agent/1.0; "
                    "+termux)"
                )
            }
        )

        with urllib.request.urlopen(request, timeout=15) as response:

            content_type = response.headers.get("Content-Type", "")

            if (
                content_type
                and "text" not in content_type
                and "html" not in content_type
                and "json" not in content_type
                and "xml" not in content_type
            ):
                return {
                    "ok": False,
                    "url": url,
                    "error": (
                        "Content-Type '" + content_type + "' nie "
                        "wygląda na stronę tekstową (obraz/plik "
                        "binarny?) — pomijam pobieranie treści."
                    )
                }

            raw = response.read(max_bytes)
            final_url = response.geturl()

    except urllib.error.HTTPError as e:

        return {
            "ok": False,
            "url": url,
            "error": "HTTP " + str(e.code) + ": " + str(e.reason)
        }

    except Exception as e:

        return {
            "ok": False,
            "url": url,
            "error": type(e).__name__ + ": " + str(e)
        }

    charset_match = _HTML_CHARSET.search(content_type or "")
    charset = charset_match.group(1) if charset_match else "utf-8"

    try:
        page = raw.decode(charset, errors="replace")
    except Exception:
        page = raw.decode("utf-8", errors="replace")

    title_match = _HTML_TITLE_TAG.search(page)
    title = (
        html.unescape(title_match.group(1)).strip()
        if title_match else ""
    )

    body = _HTML_STRIP_BLOCK_TAGS.sub(" ", page)
    body = _HTML_COMMENT.sub(" ", body)
    body = _HTML_ANY_TAG.sub(" ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    body = "\n".join(
        line.strip() for line in body.splitlines() if line.strip()
    )

    return {
        "ok": True,
        "url": final_url,
        "title": short(title, 200),
        "text": short(body, max_chars)
    }


def researcher_web_search(
    researcher_response,
    original_context
):
    """
    Pozwala RESEARCHEROWI zażądać jednej z dwóch rzeczy, sam
    wykonywanych przez Python (nie przez Gemini/telefon):

    - WEB_SEARCH: <zapytanie> — wyszukiwanie w sieci przez moduł
      web_search.py (zwraca listę wyników — tytuły/linki/skróty,
      jak wyszukiwarka).
    - WEB_FETCH: <url> — pobranie TREŚCI konkretnej strony
      bezpośrednio przez Python (patrz _fetch_url_text), gdy
      RESEARCHER już wie, którą stronę chce przeczytać, a nie
      dopiero jej szuka. Nie wymaga otwartej karty w Chrome na
      telefonie — to inna droga niż chrome_tabs/chrome_inspect.

    RESEARCHER nie wykonuje żadnego z nich bezpośrednio. Python
    wykonuje wybraną akcję i przekazuje wynik z powrotem do TEJ
    SAMEJ sesji RESEARCHER.
    """

    text = str(researcher_response or "").strip()

    fetch_match = re.search(
        r"WEB_FETCH\s*:\s*(\S+)",
        text,
        re.IGNORECASE
    )

    search_match = re.search(
        r"WEB_SEARCH\s*:\s*(.+)",
        text,
        re.IGNORECASE
    )

    if fetch_match:

        action = "WEB FETCH"
        query = fetch_match.group(1).strip().strip('"\'')

        if not query:
            return text

        log(
            "RESEARCHER",
            "WEB FETCH -> " + query
        )

        action_data = _fetch_url_text(query)

        log(
            "RESEARCHER",
            "WEB FETCH RESULT: "
            + (
                "OK (" + str(len(action_data.get("text", ""))) + " znaków)"
                if action_data.get("ok")
                else "BŁĄD: " + str(action_data.get("error"))
            )
        )

    elif search_match:

        action = "WEB SEARCH"
        query = search_match.group(1).strip()

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

            action_data = web_search(
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

            action_data = {
                "ok": False,
                "query": query,
                "results": [],
                "error":
                    type(e).__name__
                    + ": "
                    + str(e)
            }

        else:

            log(
                "RESEARCHER",
                "WEB SEARCH RESULT: "
                + str(
                    action_data.get(
                        "count",
                        len(
                            action_data.get(
                                "results",
                                []
                            )
                        )
                    )
                )
            )

    else:

        return text

    # ========================================================
    # PRZEKAZANIE WYNIKU DO TEJ SAMEJ SESJI RESEARCHER
    # ========================================================

    followup = f"""
{action} RESULT.

To jest rzeczywisty wynik {"pobrania strony" if fetch_match else "wyszukiwania"}
wykonanego przez Python (nie przez Ciebie, nie przez Gemini).

Nie wykonuj żadnych innych narzędzi.

Przeanalizuj WYŁĄCZNIE poniższy wynik.

{"URL" if fetch_match else "QUERY"}:
{query}

RESULT:
{json.dumps(
    action_data,
    ensure_ascii=False,
    indent=2
)}

ORYGINALNY KONTEKST:
{short(original_context, 5000)}

Teraz przygotuj finalną odpowiedź RESEARCHER-a.

Nie wymyślaj faktów.
Jeżeli wyniku nie wystarcza do potwierdzenia informacji,
powiedz to wyraźnie.

Nie zwracaj ponownie WEB_SEARCH/WEB_FETCH, chyba że naprawdę
potrzebna jest kolejna akcja.
"""

    try:

        final_response = deepseek(
            "RESEARCHER",
            followup
        )

    except Exception as e:

        log(
            "RESEARCHER",
            action + " FOLLOWUP ERROR: "
            + type(e).__name__
            + ": "
            + str(e)
        )

        return (
            text
            + "\n\n" + action + " RESULT:\n"
            + json.dumps(
                action_data,
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


def estimate_progress(goal, chrome_text=None, android_text=None):
    """
    Pyta PROGRESS_ESTIMATOR o procentową ocenę realizacji celu na
    podstawie kilku ostatnio zakończonych zadań. Zwraca None, jeśli
    nie ma jeszcze żadnej historii albo odpowiedź nie sparsowała
    się do sensownego JSON — nigdy nie przerywa głównej pętli
    agenta z tego powodu.

    chrome_text/android_text (opcjonalne) — patrz consult_team():
    pozwala run_agent() przekazać JUŻ pobrany w tym kroku stan
    zamiast tej funkcji odpytującej urządzenie po raz kolejny.
    """

    summaries = _recent_task_summaries(8)

    if not summaries:
        return None

    # WCZEŚNIEJ ten prompt zawierał WYŁĄCZNIE własne raporty Gemini
    # z poprzednich zadań — czyli to, co Gemini SAM o sobie
    # napisało, nie faktyczny stan urządzenia. Zaobserwowany
    # problem: ocena procentowa "nie widziała", że np. jakieś okno
    # jest faktycznie otwarte na ekranie, bo nigdy nie dostawała
    # świeżego stanu Chrome/Androida — tylko cudze, potencjalnie
    # naciągnięte podsumowania. Dokładamy prawdziwy, świeżo pobrany
    # stan (te same funkcje, których używa MAIN każdy krok), żeby
    # ocena miała choć trochę oparcia w rzeczywistości, nie tylko
    # w tym, co ktoś inny zadeklarował.
    checklist_summary = _checklist_summary_block()
    checklist_block = (
        "\n" + checklist_summary + "\n"
    ) if checklist_summary else ""

    # Ten sam mechanizm adaptacyjnej treści co w consult_team()/
    # main_decide() (v87/v88) — świeży stan Chrome/Androida tylko
    # gdy CEL faktycznie ich dotyczy, zamiast zawsze marnować ~3000
    # znaków na ocenę postępu celu, który nie ma nic wspólnego z
    # przeglądarką ani ekranem telefonu.
    device_state_block = ""

    _resolved_chrome_text_for_progress = (
        chrome_text if chrome_text is not None else chrome_summary()
    )

    # Bez last_result (nieprzekazywanego do tej funkcji) trzymamy tu
    # WYŁĄCZNIE statyczne dopasowanie CELU — patrz komentarz przy
    # _chrome_relevant_now() o unikaniu "lepkiego" spamowania stanem
    # Chrome; ocena procentowa jest niższej stawki niż decyzje
    # PLANNER/ENGINEER/MAIN, więc nie potrzebuje dynamicznego wyjątku.
    _chrome_relevant_for_progress = _goal_mentions_chrome(goal)

    if _chrome_relevant_for_progress or _goal_mentions_android(goal):

        device_state_block = (
            "\nAKTUALNY, ŚWIEŻO POBRANY STAN URZĄDZENIA (pokazuje "
            "TYLKO to co jest na ekranie TERAZ — patrz punkt 3 "
            "Twojego prompta systemowego o przejściowych "
            "czynnościach, zanim to wykorzystasz do oceny):\n"
        )

        if _chrome_relevant_for_progress:
            device_state_block += (
                "\nAKTUALNY CHROME:\n"
                + short(
                    _resolved_chrome_text_for_progress,
                    1500
                ) + "\n"
            )

        if _goal_mentions_android(goal):
            device_state_block += (
                "\nAKTUALNY ANDROID:\n"
                + short(
                    android_text if android_text is not None
                    else android_summary(),
                    1500
                ) + "\n"
            )

    prompt = f"""
CEL:
{goal}

OSTATNIE ZADANIA (od najstarszego do najnowszego, WŁASNE raporty
Gemini — traktuj je z rezerwą, mogą być niedokładne):
{json.dumps(summaries, ensure_ascii=False, indent=2)}
{checklist_block}
{device_state_block}
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

# Realna odpowiedź (z web_search RESEARCHERA) czekająca na to, żeby
# przekazać ją Wojtkowi przy JEGO następnej turze — patrz consult_team().
# Na wyraźną prośbę użytkownika: Wojtek ma dostawać PRAWDZIWE,
# zweryfikowane odpowiedzi na to, o co pyta/co twierdzi, zamiast
# sztywnego szablonu — ale BEZ tworzenia własnych zadań/wyszukiwań
# (to zdublowałoby pracę RESEARCHERA). Zamiast tego jego pytanie
# "dopina się" do wyszukiwania, które RESEARCHER i tak robi w tym
# samym kroku, a skrócony wynik wraca do Wojtka przy kolejnej okazji.
_wojtek_pending_answer = None

# Werdykt Marka/CRITIC z POPRZEDNIEGO kroku, czekający na to, żeby
# trafić do Tomka/PLANNERA przy jego następnej turze.
#
# ZAOBSERWOWANY REALNY PROBLEM (log 2026-08-28, cel "zadzwoń do
# Beaty", KROKI 6-13): Marek zablokował plan Tomka SZEŚĆ razy z
# rzędu, za każdym razem pisząc wprost o Tomku ("Plan Tomka ma trzy
# krytyczne luki", "Tomek całkowicie ignoruje trzy otwarte błędy",
# "Plan Tomka NADAL ignoruje..."). Tomek nie miał JAK się o tym
# dowiedzieć: wyjście CRITIC-a trafiało wyłącznie do MAIN-a, a każda
# rola ma własną, odrębną rozmowę z DeepSeekiem — Tomek widział
# swoje poprzednie wiadomości, ale nigdy ani jednego słowa Marka.
# Planował więc dalej obok tego samego zarzutu, bo strukturalnie nie
# mógł go usłyszeć.
#
# To jest dokładnie ta "normalna komunikacja zespołu", o którą prosił
# użytkownik: gdy recenzent mówi "twój plan pomija X", autor planu to
# słyszy i poprawia. Przekazujemy TEKST, KTÓRY I TAK JUŻ POWSTAŁ —
# zero dodatkowych wywołań DeepSeeka (użytkownik wprost sygnalizował
# obawę o blokady strony przy zbyt częstych zapytaniach).
_critic_verdict_for_planner = None


def _condense_last_result_for_team(last_result, limit=2500):
    """
    Buduje ZWIĘZŁE, czytelne podsumowanie last_result zamiast
    surowego short(json.dumps(last_result), 4500).

    Poprzednie podejście obcinało zserializowany JSON od KOŃCA
    łańcucha znaków (patrz short()). Problem: w słowniku
    GEMINI_TOOL_ERROR klucz "tool_result" (zawierający m.in.
    "stderr" — czyli zwykle FAKTYCZNY powód błędu) leży w JSON-ie
    PO kluczach "tool"/"arguments", a samo "stdout" (do 6000 znaków
    z execute_shell) leży PRZED "stderr". Przy dłuższym stdout
    realny błąd bywał więc ucięty, zanim w ogóle dotarł do zespołu
    — dużo tekstu, ale nie ten, który był diagnostycznie ważny.
    Dodatkowo user prosił wprost o mniej surowych logów przy
    zachowaniu wzajemnego zrozumienia ról.

    Tutaj wyciągamy NAJPIERW to, co faktycznie tłumaczy, co się
    stało (status, komunikat/błąd, stderr narzędzia), a dopiero
    potem, jeśli zostało miejsce, resztę (stdout, pełny raport
    Gemini).
    """

    if not isinstance(last_result, dict):
        return short(
            json.dumps(last_result, ensure_ascii=False),
            limit
        )

    parts = [
        "status=" + str(last_result.get("status", "?"))
        + " ok=" + str(last_result.get("ok"))
    ]

    tool = last_result.get("tool")

    if tool:
        parts.append("narzędzie: " + str(tool))
        args = last_result.get("arguments")
        if args:
            parts.append(
                "argumenty: "
                + short(json.dumps(args, ensure_ascii=False), 300)
            )

    message = last_result.get("message") or last_result.get("error")

    if message:
        parts.append("komunikat: " + short(str(message), 500))

    # Zaobserwowany realny bug (log 2026-08-27): last_result po
    # NEED_USER_LOGIN niesie "note"/"user_provided_value" (np. klucz
    # API, który użytkownik wkleił wprost w okienku Enter — patrz
    # _handle_need_user_login()), ale ta funkcja miała STAŁĄ listę
    # kluczy i nie znała żadnego z nich — MAIN (który dostaje surowy
    # JSON last_result) widział wklejoną wartość, ale reszta zespołu
    # (PLANNER/ENGINEER, którzy dostają WYŁĄCZNIE ten skrót) nie
    # widziała jej wcale, mimo że to ENGINEER faktycznie pisze kod,
    # który miałby tę wartość wykorzystać.
    note = last_result.get("note")

    if note:
        parts.append("uwaga: " + short(str(note), 800))

    user_provided_value = last_result.get("user_provided_value")

    if user_provided_value:
        parts.append(
            "wartość wklejona przez użytkownika: "
            + short(str(user_provided_value), 800)
        )

    # Ścieżka jako OSOBNA pozycja, nie tylko wewnątrz "note" — note
    # jest skracana do 800 znaków, a to jedyna informacja, dzięki
    # której Gemini w ogóle może dosięgnąć wklejonej wartości (patrz
    # USER_PROVIDED_VALUE_FILE). Ucięcie jej przez limit długości
    # cicho przywróciłoby dokładnie ten błąd, który naprawia.
    value_file = last_result.get("user_provided_value_file")

    if value_file:
        parts.append(
            "wklejona wartość jest też ZAPISANA W PLIKU "
            + str(value_file)
            + " — w TASKu dla Gemini podawaj tę ŚCIEŻKĘ (np. "
            "`KEY=$(cat " + str(value_file) + ")`), nie samą wartość"
        )

    tool_result = last_result.get("tool_result")

    if isinstance(tool_result, dict):
        tr_error = (
            tool_result.get("error")
            or tool_result.get("stderr")
            or tool_result.get("stderr_partial")
        )
        if tr_error:
            parts.append("błąd narzędzia: " + short(str(tr_error), 800))

        tr_stdout = (
            tool_result.get("stdout")
            or tool_result.get("stdout_partial")
        )
        if tr_stdout and str(tr_stdout).strip():
            parts.append("stdout: " + short(str(tr_stdout), 500))

    report = last_result.get("report")

    if report:
        parts.append("raport Gemini: " + short(str(report), 1200))

    tool_calls = last_result.get("tool_calls")

    if tool_calls:
        parts.append("liczba wywołań narzędzi: " + str(tool_calls))

    # CO GEMINI FAKTYCZNIE WYWOŁAŁO — zapis zbierany przez Pythona,
    # nie deklaracja Gemini.
    #
    # ZAOBSERWOWANY REALNY PROBLEM (analiza kodu + logi): tool_trace
    # był zbierany (gemini_execute_task), zapisywany do checklisty i
    # NIGDY NIKOMU NIE POKAZYWANY. Zespół oceniał krok wyłącznie na
    # podstawie PROZY Gemini ("raport Gemini: CO ZROBIŁEM: 1... 2...")
    # — a CRITIC ma w swoim prompcie WPROST zadanie wykrywania
    # fabrykacji ("raport podał 'Python 3.11.8', a realnie odczytany
    # plik pokazywał 3.14.6") i nie miał ŻADNEGO niepodrabialnego
    # źródła, z którym mógłby tę prozę porównać.
    #
    # Ta lista jest dokładnie takim źródłem: pochodzi z faktycznego
    # dyspozytora narzędzi, więc Gemini nie ma jak jej "opowiedzieć".
    # Trzymana zwięźle (nazwa + czy się udało), żeby nie stała się
    # kolejnym szumem — przy długich seriach zwijana do zliczeń.
    tool_trace = last_result.get("tool_trace")

    if isinstance(tool_trace, list) and tool_trace:

        def _name_of(entry):
            return str(entry.get("tool", "?")) if isinstance(entry, dict) else "?"

        def _ok_of(entry):
            return entry.get("ok") if isinstance(entry, dict) else None

        failed = sum(1 for e in tool_trace if _ok_of(e) is False)

        if len(tool_trace) <= 12:
            rendered = ", ".join(
                _name_of(e) + ("" if _ok_of(e) is not False else " [BŁĄD]")
                for e in tool_trace
            )
        else:
            counts = {}
            for e in tool_trace:
                counts[_name_of(e)] = counts.get(_name_of(e), 0) + 1
            rendered = ", ".join(
                name + " x" + str(n)
                for name, n in sorted(
                    counts.items(),
                    key=lambda kv: -kv[1]
                )
            )

        parts.append(
            "co Gemini FAKTYCZNIE wywołało (zapis Pythona, nie jego "
            "własna proza) — wywołań: "
            + str(len(tool_trace))
            + ", nieudanych: " + str(failed) + " -> "
            + short(rendered, 600)
        )

    return short("\n".join(parts), limit)


# ADAPTACYJNA TREŚĆ (2026-08-24, na wyraźną prośbę użytkownika —
# "dynamiczne skracanie/rozszerzanie instrukcji zależnie od
# sytuacji", rozszerzone stopniowo na cały zespół DeepSeek).
# Wspólne dla consult_team() i main_decide(): czy CEL (stały przez
# całą sesję, więc liczony raz na wywołanie, nie per-token) w ogóle
# dotyczy Androida/telefonu albo Chrome/przeglądarki — jeśli nie,
# nie ma sensu wysyłać do każdej roli/MAIN-a wielotysięcznych
# zrzutów stanu, których treść jest dla tego celu kompletnie
# nieistotna. Fałszywie ujemne dopasowanie nie jest stratą: CEL
# nadal jest widoczny w pełni, więc rola może samodzielnie poprosić
# o więcej stanu przez zwykły TASK, jeśli faktycznie się okaże
# potrzebny.
_GOAL_ANDROID_KEYWORDS = (
    "android", "kalkulator", "zegar", "kalendarz", "aplikacj",
    "kliknij", "klikni", "wpisz", "przycisk", "ekran", "telefon",
    "urządzeni", "urzadzeni", "apk", "gra", "gry", "grę", "gre",
)

_GOAL_CHROME_KEYWORDS = (
    "chrome", "przeglądark", "przegladark", "karta", "kart",
    "stron", "url", "http", "www.", "wyszukiwark", "google",
)


def _goal_mentions_android(goal):
    goal_lower = str(goal or "").lower()
    return any(kw in goal_lower for kw in _GOAL_ANDROID_KEYWORDS)


def _goal_mentions_chrome(goal):
    goal_lower = str(goal or "").lower()
    return any(kw in goal_lower for kw in _GOAL_CHROME_KEYWORDS)


# Zaobserwowany realny bug (log 2026-08-28, cel: integracja głosowa +
# telefon do Beaty): CEL nie wspominał wprost słów "chrome"/
# "przeglądarka"/"URL" (_goal_mentions_chrome zwracało False), więc
# PLANNER, ENGINEER i nawet sam MAIN NIGDY nie dostawali stanu
# Chrome — mimo że w międzyczasie NEED_USER_LOGIN otworzył konsolę
# Twilio, użytkownik się zalogował, a Account SID był WIDOCZNY WPROST
# w adresie URL karty (".../account/AC18e2f65e69db8b12faee23e58634
# cd6b"). Zespół tego nigdy nie zobaczył.
#
# PIERWSZA wersja tej poprawki uznawała Chrome za "istotny" za każdym
# razem, gdy AKTUALNY stan pokazywał jakąkolwiek prawdziwą stronę —
# ale karta w Chrome zostaje otwarta na tym samym adresie przez CAŁĄ
# resztę sesji (nic jej nie zamyka), więc taki warunek byłby "lepki":
# spamowałby cały zespół tym samym zrzutem stanu Chrome na KAŻDYM
# kolejnym kroku aż do końca sesji, dokładnie to marnotrawstwo
# kontekstu, któremu miał zapobiegać cały mechanizm z v87 (na
# wyraźną uwagę użytkownika po wdrożeniu). Naprawiono: Chrome jest
# "istotny" dynamicznie TYLKO na jeden krok — ten bezpośrednio po
# odpowiedzi użytkownika na NEED_USER_LOGIN (last_result.status ==
# USER_RESPONDED_TO_LOGIN_PROMPT) — bo to jedyny moment, w którym
# wiadomo, że coś w Chrome mogło się właśnie zmienić z powodu akcji
# człowieka. To samo naturalnie "wygasa": już następny krok ma inny
# last_result, więc dodatkowy stan Chrome nie ciągnie się bez końca.
def _chrome_relevant_now(goal, last_result=None):

    return (
        _goal_mentions_chrome(goal)
        or (
            isinstance(last_result, dict)
            and last_result.get("status")
            == "USER_RESPONDED_TO_LOGIN_PROMPT"
        )
    )


# Na wyraźną prośbę użytkownika: Ola nie ma tylko streszczać do JEDNEJ
# wspólnej wiadomości dla wszystkich — ma umieć zaadresować coś
# WYŁĄCZNIE do konkretnej osoby, tak jak człowiek w zespole robi, gdy
# wie że dana uwaga dotyczy tylko jednej roli. Zamiast dawać jej OSOBNE
# wywołanie DeepSeeka na każdą rolę (użytkownik wyraźnie chce oszczędzać
# — opendeep steruje stroną czatu, nie oficjalnym API, więc zbyt
# częste wywołania ryzykują blokadę strony), robi to w TEJ SAMEJ,
# jednej konsultacji: jeśli ma coś do powiedzenia tylko jednej osobie,
# dopisuje to na końcu jako linię "DLA TOMKA:"/"DLA KAMILA:"/
# "DLA MARKA:"/"DLA BARTKA:" — Python to wycina i kieruje WYŁĄCZNIE do
# tej roli, reszta zespołu tego nie widzi. Wojtek celowo NIE jest tu
# uwzględniony — jego rola ma pozostać wolna od szczegółów
# technicznych (patrz WOJTEK_PROMPT/_ROLE_ACCOUNT), więc nie dostaje
# adresowanych dopisków Oli.
_OLA_ROLE_CALLOUT_RE = re.compile(
    r'DLA (TOMKA|KAMILA|MARKA|BARTKA):\s*',
    re.IGNORECASE
)

_OLA_CALLOUT_NAME_TO_ROLE = {
    "TOMKA": "PLANNER",
    "KAMILA": "RESEARCHER",
    "MARKA": "CRITIC",
    "BARTKA": "ENGINEER",
}


def _split_ola_translation_by_role(text):

    text = str(text or "")

    matches = list(_OLA_ROLE_CALLOUT_RE.finditer(text))

    if not matches:
        return text.strip(), {}

    general = text[:matches[0].start()].strip()

    callouts = {}

    for i, m in enumerate(matches):

        role_key = _OLA_CALLOUT_NAME_TO_ROLE.get(m.group(1).upper())

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()

        if role_key and chunk:
            callouts[role_key] = (
                callouts.get(role_key, "") + (" " if role_key in callouts else "") + chunk
            )

    return general, callouts


def consult_team(
    goal,
    last_result,
    step=1,
    chrome_text=None,
    android_text=None
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

    Dlatego PLANNER, CRITIC i ENGINEER (te trzy
    bezpośrednio napędzają decyzję MAIN) są pytane na KAŻDYM kroku,
    ale RESEARCHER i BROWSER — które w praktyce rzadko mają coś
    nowego do powiedzenia z kroku na krok — tylko co 3. krok, plus
    zawsze od razu po świeżym błędzie narzędzia (wtedy RESEARCHER
    faktycznie może pomóc znaleźć przyczynę). W pominiętych krokach
    MAIN dostaje ich OSTATNIĄ znaną odpowiedź z jasną adnotacją, że
    jest nieaktualna — lepsze to niż pusty kontekst, ale MAIN wie,
    że nie powinien na niej ślepo polegać.

    KOLEJNOŚĆ (jak w ludzkim zespole — najpierw ustal fakty, potem
    planuj na ich podstawie, nie odwrotnie): RESEARCHER -> PLANNER
    -> BROWSER -> ENGINEER -> CRITIC -> (MAIN, poza tą
    funkcją). Wcześniej PLANNER planował PRZED RESEARCHEREM, więc
    świeże ustalenia trafiały dopiero do NASTĘPNEGO kroku — o krok
    za późno na to, żeby faktycznie wpłynąć na plan, którego
    dotyczyły.

    chrome_text/android_text (opcjonalne, 2026-08-24): run_agent()
    pobiera świeży stan Chrome/Androida RAZ na krok (do logu [STATE])
    i przekazuje TE SAME napisy tutaj, zamiast każdej funkcji
    (consult_team/main_decide/estimate_progress) osobno odpytującej
    urządzenie o dokładnie ten sam stan — realny, zaobserwowany
    problem: to były do 4 NIEZALEŻNYCH, żywych zapytań ADB/CDP na
    jeden krok agenta, mierzalnie spowalniających pętlę bez żadnej
    korzyści (stan i tak nie zmienia się między nimi w obrębie tego
    samego kroku). Gdy nie podano (np. wywołanie bezpośrednio, jak w
    testach), funkcja sama pyta urządzenie — zachowanie identyczne
    jak wcześniej.
    """

    global _wojtek_pending_answer
    global _critic_verdict_for_planner

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

        tool_warnings = last_result.get("tool_warnings") or []

        if tool_warnings:
            tool_hint += (
                "\n\n⚠️ SYGNAŁY Z NARZĘDZI W OSTATNIM ZADANIU "
                "(wykryte automatycznie w kodzie, NIEZALEŻNIE od "
                "tego, co Gemini napisało w swoim raporcie — "
                "traktuj jako fakty, nawet jeśli raport poniżej "
                "brzmi na sukces):\n"
                + "\n".join(
                    "- " + str(w) for w in tool_warnings[:8]
                )
            )

    progress_snapshot = _goal_progress_snapshot(goal)
    progress_block = ("\n" + progress_snapshot + "\n") if progress_snapshot else ""

    checklist_summary = _checklist_summary_block()
    checklist_block = ("\n" + checklist_summary + "\n") if checklist_summary else ""

    # Pamięć zespołu o już wypróbowanych podejściach — patrz
    # APPROACHES_FILE. Doklejana do checklisty, bo to ta sama klasa
    # informacji: twarde fakty zebrane przez Pythona, nie deklaracje.
    _approaches_summary = _approaches_summary_block()

    if _approaches_summary:
        checklist_block += "\n" + _approaches_summary + "\n"

    # Na wyraźną prośbę użytkownika (2026-08-27): reszta zespołu od
    # v116/v132 rozmawia po ludzku, ale sam OSTATNI RAPORT był
    # mechanicznym zlepkiem Pythona ("status=... ok=... narzędzie:
    # ..."), nie prawdziwym zdaniem — "sztywny środek" mimo ludzkiej
    # otoczki. Ola/BROWSER była też NAJMNIEJ wykorzystywaną rolą w
    # uniwersalnym bocie (konsultowana tylko gdy CEL dotyczył
    # przeglądarki) — dla większości celów nigdy się nie odzywała.
    # Przerobiono jej rolę: na KAŻDYM kroku (nie tylko co 3.) tłumaczy
    # surowy, techniczny wynik na proste, ludzkie zdania — to ta
    # wersja trafia do reszty zespołu jako OSTATNI RAPORT. Ostre
    # ostrzeżenia (tool_hint — "nie proponuj tego samego podejścia")
    # zostają doklejone OSOBNO, dosłownie, PO tłumaczeniu — żeby
    # parafraza nigdy nie osłabiła krytycznego ostrzeżenia.
    # Na wyraźną prośbę użytkownika (2026-08-27): reszta zespołu (Ola
    # przy tłumaczeniu, Tomek/Kamil/Wojtek/Marek przy czytaniu OSTATNI
    # RAPORT) nie ma już "spamowanej" dosłownej wklejonej wartości
    # (np. klucza API) — potrzebuje jej WYŁĄCZNIE Bartek/ENGINEER,
    # który faktycznie pisze kod i jej użyje (patrz
    # engineer_value_handoff niżej). Zamiast polegać na prośbie do
    # LLM "nie cytuj tego" (Ola i tak widziałaby sekret w swoim
    # wejściu i mogłaby go przypadkiem powtórzyć), REDAGUJEMY wartość
    # z materiału, zanim w ogóle do niej trafi — sekret fizycznie nie
    # istnieje w tym, co widzi Ola/reszta zespołu.
    user_value = (
        last_result.get("user_provided_value")
        if isinstance(last_result, dict) else None
    )

    last_result_for_team = last_result

    if user_value and isinstance(last_result, dict):
        last_result_for_team = dict(last_result)
        last_result_for_team["user_provided_value"] = (
            "(wartość ukryta tutaj — przekazana bezpośrednio "
            "Bartkowi, reszta zespołu jej nie potrzebuje)"
        )

    raw_report_material = _condense_last_result_for_team(last_result_for_team)

    human_report = deepseek(
        "BROWSER",
        f"""
Przetłumacz poniższy techniczny raport na proste, ludzkie zdania
(1-3 zdania) — jakbyś opowiadała koledze z zespołu, co się stało.
Bez żargonu i nazw wewnętrznych narzędzi, chyba że są naprawdę
potrzebne do zrozumienia. Nie oceniaj, nie planuj kolejnego kroku —
tylko streść fakty.

Jeśli w raporcie jest coś, co ma znaczenie TYLKO dla jednej,
konkretnej osoby z zespołu (np. szczegół istotny wyłącznie dla
Bartka, który pisze kod, albo coś, co Marek powinien konkretnie
zweryfikować przy ocenie planu) — dopisz to na KOŃCU jako osobną
linię zaczynającą się dosłownie od "DLA TOMKA:", "DLA KAMILA:",
"DLA MARKA:" albo "DLA BARTKA:" (tylko dla osoby, dla której to
faktycznie ważne — możesz nie pisać żadnej, jeśli nic takiego nie
ma; NIE pisz do Wojtka, on nie zajmuje się szczegółami technicznymi).

CEL:
{goal}

SUROWY RAPORT:
{raw_report_material}
"""
    )

    # Jeśli tłumaczenie się nie powiodło (pusta odpowiedź), nie
    # zostawiamy zespołu bez niczego — wracamy do surowego zlepku.
    readable_report_general, ola_role_callouts = _split_ola_translation_by_role(
        human_report
    )

    readable_report = readable_report_general if human_report else ""

    # Deterministyczne (nie przez LLM) przekazanie DOSŁOWNEJ wartości
    # WYŁĄCZNIE Bartkowi/ENGINEER — patrz komentarz przy
    # last_result_for_team wyżej. Doklejane do jego `extra` niżej, przy
    # wywołaniu ENGINEER (nie trafia do core_context, więc reszta
    # zespołu jej nie widzi).
    engineer_value_handoff = ""

    if user_value:

        engineer_value_handoff = (
            "\n\nDLA CIEBIE (Bartek) — użytkownik wkleił tę wartość "
            "DOSŁOWNIE, użyj jej bezpośrednio (np. zapisz do "
            "właściwego pliku), nie proś o nią ponownie:\n"
            + str(user_value)
        )

        if "\n" in str(user_value):
            engineer_value_handoff += (
                "\n\nUWAGA: ta wartość ma WIELE LINII — to może być "
                "skopiowany fragment całej strony, nie sama wartość. "
                "PRZEJRZYJ WSZYSTKIE linie i znajdź tę, która "
                "faktycznie wygląda jak oczekiwana wartość (długi "
                "ciąg losowych znaków alfanumerycznych, bez spacji), "
                "zamiast zakładać że cały tekst to jedna wartość."
            )

    # Fakty wspólne dla WSZYSTKICH ról — to jedyna część, która
    # faktycznie musi być identyczna, żeby zespół "rozumiał się
    # nawzajem" (patrz prośba użytkownika). Stan Chrome/Android
    # NIE wchodzi tu już na sztywno — każda rola dostaje TYLKO ten
    # fragment stanu, który faktycznie dotyczy jej roli (patrz
    # _ROLE_CONTEXT_BLOCKS niżej). Wcześniej RESEARCHER i BROWSER
    # dostawały bajt-w-bajt ten sam pełny blok (CEL + raport +
    # Chrome + Android) — czysty spam bez różnicowania ról.
    # Gdy ostatni krok SIĘ WYWALIŁ, do ludzkiego streszczenia Oli
    # DOKLEJAMY surowe fakty techniczne — zamiast je nimi zastępować.
    #
    # ZAOBSERWOWANY REALNY PROBLEM (analiza kodu + logi 2026-08-28):
    # ta sekcja budowała się jako `readable_report or
    # raw_report_material`, czyli wystarczyło, że Ola cokolwiek
    # napisała, a WSZYSTKIE surowe szczegóły znikały. Przy pierwszej
    # porażce narzędzia (najczęstszy przypadek — tool_hint pojawia
    # się dopiero przy POWTÓRZONYCH) zespół nie widział ani
    # `SyntaxError: invalid syntax`, ani dokładnej komendy, ani
    # stderr — wyłącznie zdanie w rodzaju "nie udało się uruchomić
    # skryptu". Najbardziej cierpiał na tym Bartek, który ma
    # napisać POPRAWKĘ, ale nie dostawał treści błędu, który
    # naprawia.
    #
    # Użytkownik ujął to wprost: "kiedy błąd to błąd i każdy dostaje
    # odpowiedzi" — w normalnej rozmowie zespołu mówi się "skrypt
    # padł" I POKAZUJE log, a nie samo "coś poszło nie tak".
    # Streszczenie Oli zostaje (to ona robi z tego zdanie po
    # ludzku), surowe fakty idą pod spodem. Doklejane WYŁĄCZNIE przy
    # faktycznym błędzie, żeby nie rozdymać każdego udanego kroku.
    error_details_block = ""

    if (
        readable_report
        and isinstance(last_result, dict)
        and (
            last_result.get("ok") is False
            or last_result.get("status") == "GEMINI_TOOL_ERROR"
        )
    ):
        error_details_block = (
            "\n\nDOKŁADNIE TO, CO ZWRÓCIŁO NARZĘDZIE (surowe, "
            "nieprzetworzone — na tym opieraj poprawkę, nie na samym "
            "streszczeniu powyżej):\n"
            + _condense_last_result_for_team(
                last_result_for_team,
                limit=1400
            )
        )

    core_context = f"""
CEL:
{goal}
{progress_block}{checklist_block}
OSTATNI RAPORT:
{readable_report or raw_report_material}{error_details_block}
{tool_hint}
"""

    chrome_block = (
        "\nAKTUALNY CHROME:\n"
        + short(
            chrome_text if chrome_text is not None else chrome_summary(),
            2000
        )
        + "\n"
    )

    android_block = (
        "\nAKTUALNY ANDROID:\n"
        + short(
            android_text if android_text is not None else android_summary(),
            2000
        )
        + "\n"
    )

    # Krótkie przypomnienie roli DOPISANE DO WIADOMOŚCI (nie tylko
    # do stałego system_prompt sesji) — użytkownik prosił wprost,
    # żeby każda rola wiedziała, że ma INNĄ rolę niż pozostałe, nie
    # tylko dostawała identyczny zrzut faktów.
    # UWAGA (zaobserwowany realny problem, 2026-08-27): system_prompt
    # każdej roli od v116 zaczyna się od ludzkiego imienia ("Nazywasz
    # się Tomek..."), ale to przypomnienie roli — DOKLEJANE DO KAŻDEJ
    # WIADOMOŚCI, nie tylko do system_prompt — nadal używało sztywnej,
    # mechanicznej etykiety ("TWOJA ROLA: PLANNER", "to CRITIC" itd.).
    # Efekt: cała "ludzka" ramka wracała tylnymi drzwiami na każdym
    # kroku. Poprawiono, żeby konsekwentnie używać imion wszędzie.
    _ROLE_FOCUS_REMINDER = {
        "RESEARCHER": (
            "Tu Kamil. Szukaj w sieci KONKRETNEJ "
            "przyczyny/rozwiązania problemu poniżej. Nie planuj "
            "kolejnego kroku (to Tomek) i nie oceniaj planu (to "
            "Marek)."
        ),
        "PLANNER": (
            "Tu Tomek. Zaproponuj JEDEN konkretny "
            "następny krok na podstawie stanu i ustaleń Kamila "
            "poniżej. Nie szukaj w sieci (to Kamil) i nie "
            "oceniaj własnego planu (to Marek)."
        ),
        "BROWSER": (
            "Tu Ola. Oceń WYŁĄCZNIE stan przeglądarki "
            "Chrome poniżej — czy karty/adresy mają sens względem "
            "celu. Nie komentuj stanu Androida ani nie planuj "
            "kroków spoza przeglądarki."
        ),
        "ENGINEER": (
            "Tu Bartek. Dostarcz konkretny "
            "kod/rozwiązanie techniczne na podstawie planu Tomka "
            "i ustaleń Kamila poniżej."
        ),
        "CRITIC": (
            "Tu Marek. Oceń KRYTYCZNIE plan Tomka "
            "poniżej (ryzyka, błędy, brakujące dowody) na podstawie "
            "STANU FAKTYCZNEGO/checklisty powyżej — nie twórz "
            "nowego planu od zera. Surowego zrzutu ekranu Chrome/"
            "Androida celowo NIE dostajesz — Tomek już go użył do "
            "ułożenia planu poniżej; Twoja praca to ocena LOGIKI i "
            "DOWODÓW, nie ponowne odczytywanie ekranu."
        ),
    }

    def _team_context(role_name, include_chrome=False, include_android=False, extra=""):
        pieces = [
            _ROLE_FOCUS_REMINDER.get(role_name, ""),
            core_context
        ]
        if include_chrome:
            pieces.append(chrome_block)
        if include_android:
            pieces.append(android_block)
        if extra:
            pieces.append(extra)
        return "\n".join(p for p in pieces if p)

    # Role odpytywane PO KOLEI — jedno realne połączenie do
    # opendeep na raz. CRITIC i ENGINEER dostają
    # dodatkowo wyjście PLANNERA/RESEARCHERA, więc muszą i tak
    # czekać, aż tamci skończą.

    results = {}

    fresh_tool_error = (
        isinstance(last_result, dict)
        and last_result.get("status") == "GEMINI_TOOL_ERROR"
    )

    goal_needs_android = _goal_mentions_android(goal)
    goal_needs_chrome = _chrome_relevant_now(goal, last_result)

    consult_researcher = (
        (step % 3 == 1)
        or fresh_tool_error
    )

    consult_browser = (
        goal_needs_chrome
        and (step % 3 == 1)
    )

    # WOJTEK to jedyna rola, która NIE dostaje core_context (bez
    # STAN FAKTYCZNY/checklisty/tool_hint/szczegółów technicznych)
    # — użytkownik chciał kogoś, kto myśli o CELU jak zwykły
    # człowiek, bez wiedzy o Termuxie/Androidzie/Chrome/narzędziach,
    # więc dostaje WYŁĄCZNIE sam opis celu. Pytany tak samo rzadko
    # jak RESEARCHER (oszczędzanie limitu/sesji) — to rola
    # dodatkowa/inspiracyjna, nie krytyczna dla decyzji MAIN.
    consult_wojtek = (
        (step % 3 == 1)
        or fresh_tool_error
    )

    if consult_wojtek:

        # UWAGA (zaobserwowany realny problem, log 2026-08-26): Wojtek
        # dostawał ZA KAŻDYM razem BAJT-W-BAJT identyczną wiadomość
        # (sam surowy CEL, bez żadnej zmiany) — a jego sesja DeepSeek
        # jest ciągła, więc z JEGO perspektywy ktoś po prostu wklejał
        # to samo pytanie po raz drugi, trzeci... szósty. Efekt:
        # zamiast nowych pomysłów, Wojtek zaczynał odpowiadać
        # sfrustrowanym komentarzem o powtarzaniu się rozmówcy.
        # Pierwsza poprawka (dopisanie "to nie pomyłka") wciąż
        # WKLEJAŁA CAŁY CEL od nowa za każdym razem — leczyła objaw,
        # nie przyczynę. Właściwa naprawa: skoro jego sesja i tak
        # PAMIĘTA cel od pierwszego razu, kolejne pytania są krótkim,
        # naturalnym dopytaniem BEZ powtarzania celu — dokładnie tak,
        # jak wygląda prawdziwa rozmowa z kimś, kto już wie, o co
        # chodzi.
        wojtek_already_asked = "WOJTEK" in _role_response_cache

        if not wojtek_already_asked:

            wojtek_context = (
                "Oto zadanie, nad którym ktoś aktualnie pracuje:\n\n"
                + goal
                + "\n\nPodziel się swoimi pomysłami."
            )

        else:

            # Na wyraźną prośbę użytkownika: jeśli poprzednio Wojtek
            # coś twierdził/o coś pytał, i RESEARCHER (w ramach
            # własnego, i tak wykonywanego wyszukiwania — patrz niżej)
            # zdążył to sprawdzić, oddajemy mu PRAWDZIWĄ, zweryfikowaną
            # odpowiedź zamiast pustego "masz coś nowego?". Zero
            # nowego wyszukiwania stworzonego specjalnie dla Wojtka —
            # to ta sama odpowiedź, którą RESEARCHER i tak już podał
            # zespołowi.
            if _wojtek_pending_answer:

                answer_block = (
                    "Sprawdziliśmy to, o czym wspomniałeś ostatnio: "
                    + _wojtek_pending_answer
                    + "\n\n"
                )

                _wojtek_pending_answer = None

            else:

                answer_block = ""

            wojtek_context = (
                answer_block
                + "Wracam do Ciebie w tej samej sprawie — masz jakieś "
                "nowe pomysły, czy chcesz rozwinąć któryś z tych, o "
                "których już mówiłeś?"
            )

        results["WOJTEK"] = deepseek(
            "WOJTEK",
            wojtek_context
        )

        _role_response_cache["WOJTEK"] = results["WOJTEK"]

    else:

        log(
            "DEEPSEEK",
            "WOJTEK pominięty w tym kroku "
            "(oszczędzanie limitu/sesji) — "
            "użyta ostatnia znana odpowiedź."
        )

        results["WOJTEK"] = (
            "[NIEAKTUALNE — WOJTEK nie był pytany w tym kroku, "
            "poniżej jego ostatnia znana odpowiedź]\n\n"
            + _role_response_cache.get(
                "WOJTEK",
                "(WOJTEK nie był jeszcze konsultowany.)"
            )
        )

    wojtek_out = short(results.get("WOJTEK", ""), 1500)

    # RESEARCHER PRZED PLANNEREM — żeby świeże ustalenia (gdy w
    # ogóle konsultowane w tym kroku) mogły od razu wpłynąć na plan
    # z TEGO SAMEGO kroku, zamiast czekać na następny.

    if consult_researcher:

        # Jeśli Wojtek właśnie coś twierdził/o coś pytał (patrz wyżej),
        # doklejamy to do TEGO SAMEGO, i tak wykonywanego wyszukiwania
        # RESEARCHERA — bez tworzenia dla niego osobnego zapytania.
        # Wynik trafia normalnie do zespołu (jak dotychczas) ORAZ,
        # skrócony, czeka jako _wojtek_pending_answer na jego następną
        # turę (patrz wyżej) — to realna, zweryfikowana odpowiedź, nie
        # sztywny szablon.
        wojtek_extra = (
            (
                "\n\nDODATKOWO: kolega z zespołu napisał to (jeśli da "
                "się to sprawdzić w sieci, zweryfikuj i uwzględnij "
                "wynik, w przeciwnym razie zignoruj):\n" + wojtek_out
            )
            if consult_wojtek and wojtek_out
            else ""
        )

        researcher_context = _team_context(
            "RESEARCHER",
            extra=(
                wojtek_extra
                + (
                    "\n\nOD OLI (tylko dla Ciebie): "
                    + ola_role_callouts["RESEARCHER"]
                    if "RESEARCHER" in ola_role_callouts else ""
                )
            )
        )

        results["RESEARCHER"] = researcher_web_search(
            deepseek(
                "RESEARCHER",
                researcher_context
            ),
            researcher_context
        )

        _role_response_cache["RESEARCHER"] = results["RESEARCHER"]

        if wojtek_extra:
            _wojtek_pending_answer = short(results["RESEARCHER"], 400)

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

    researcher_out = short(results.get("RESEARCHER", ""), 2000)

    # Zarzut Marka do POPRZEDNIEGO planu Tomka — patrz
    # _critic_verdict_for_planner. Bez tego Tomek planuje obok tej
    # samej, powtarzanej co krok uwagi, bo nigdy jej nie widzi.
    if _critic_verdict_for_planner:

        critic_feedback_block = (
            "\n\nCO MAREK ZARZUCIŁ TWOJEMU POPRZEDNIEMU PLANOWI "
            "(przeczytaj to ZANIM zaproponujesz kolejny krok — jeśli "
            "zarzut jest słuszny, uwzględnij go; jeśli uważasz, że "
            "Marek się myli, napisz wprost dlaczego, zamiast go po "
            "cichu pomijać):\n"
            + _critic_verdict_for_planner
        )

        _critic_verdict_for_planner = None

    else:
        critic_feedback_block = ""

    results["PLANNER"] = deepseek(
        "PLANNER",
        _team_context(
            "PLANNER",
            include_chrome=goal_needs_chrome,
            include_android=goal_needs_android,
            extra=(
                "\nINFO KAMILA:\n" + researcher_out
                + "\nPOMYSŁ WOJTKA:\n" + wojtek_out
                + critic_feedback_block
                + (
                    "\n\nOD OLI (tylko dla Ciebie): "
                    + ola_role_callouts["PLANNER"]
                    if "PLANNER" in ola_role_callouts else ""
                )
            )
        )
    )

    if consult_browser:

        results["BROWSER"] = deepseek(
            "BROWSER",
            _team_context("BROWSER", include_chrome=True)
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

    results["ENGINEER"] = deepseek(
        "ENGINEER",
        _team_context(
            "ENGINEER",
            include_android=goal_needs_android,
            extra=(
                "\nPLAN TOMKA:\n" + planner_out
                + "\nINFO KAMILA:\n" + researcher_out
                + "\nPOMYSŁ WOJTKA:\n" + wojtek_out
                + engineer_value_handoff
                + (
                    "\n\nOD OLI (tylko dla Ciebie): "
                    + ola_role_callouts["ENGINEER"]
                    if "ENGINEER" in ola_role_callouts else ""
                )
            )
        )
    )

    results["CRITIC"] = deepseek(
        "CRITIC",
        _team_context(
            "CRITIC",
            extra=(
                "\nPLAN TOMKA:\n" + planner_out
                + (
                    "\n\nOD OLI (tylko dla Ciebie): "
                    + ola_role_callouts["CRITIC"]
                    if "CRITIC" in ola_role_callouts else ""
                )
            )
        )
    )

    # Werdykt Marka czeka na Tomka do NASTĘPNEGO kroku. Przekazujemy
    # go tylko wtedy, gdy faktycznie jest zastrzeżeniem (OSTRZEŻENIE/
    # BLOKUJ) — "OCENA: OK" nie wnosi nic, czego Tomek miałby
    # słuchać, a doklejanie zgody co krok byłoby tylko kolejnym
    # szumem w jego kontekście.
    _critic_out_full = results.get("CRITIC", "") or ""

    if any(
        marker in _critic_out_full.upper()
        for marker in ("BLOKUJ", "OSTRZEŻENIE")
    ):
        _critic_verdict_for_planner = short(_critic_out_full, 1200)

    return {
        "planner":   short(results.get("PLANNER", ""), 4000),
        "researcher": short(results.get("RESEARCHER", ""), 4000),
        "engineer":  short(results.get("ENGINEER", ""), 4000),
        # Pełna, nieskrócona odpowiedź ENGINEER — NIE
        # trafia do prompta MAIN (żeby nie pompować mu kontekstu),
        # ale run_agent() jej potrzebuje w całości, żeby wyciąć z
        # niej blok kodu przy write_engineer_code_to (patrz
        # extract_code_block() / obsługa TASK w run_agent()).
        "engineer_full": results.get("ENGINEER", ""),
        "critic":    short(results.get("CRITIC", ""), 4000),
        "browser":   short(results.get("BROWSER", ""), 2000),
        "wojtek":    short(results.get("WOJTEK", ""), 1500),
    }


# ============================================================
# MAIN DECISION
# ============================================================

_MAIN_ASK_ALLOWED_ROLES = (
    "PLANNER", "RESEARCHER", "CRITIC", "BROWSER", "ENGINEER"
)


def main_decide(
    goal,
    step,
    team,
    last_result,
    chrome_text=None,
    android_text=None,
    asked_followup=None
):

    # ADAPTACYJNA TREŚĆ (2026-08-24, kontynuacja v86/v87 — teraz w
    # samym MAIN-ie). Wcześniej MAIN dostawał wyjaśnienie WSZYSTKICH
    # 7 możliwych statusów przy KAŻDEJ decyzji, niezależnie od tego,
    # który status faktycznie wystąpił w TYM konkretnym OSTATNIM
    # WYNIKU (99% czasu to COMPLETED — reszta wyjaśnień to wtedy
    # czysty, nieużywany balast). Pokazujemy teraz wyjaśnienie
    # WYŁĄCZNIE dla statusu, który faktycznie jest w last_result
    # (plus zawsze krótkie COMPLETED jako punkt odniesienia, chyba
    # że to właśnie ono wystąpiło — wtedy nie ma sensu dublować).
    _status_explanations = {
        "GEMINI_TOOL_ERROR": (
            'GEMINI_TOOL_ERROR — Gemini próbował użyć narzędzia i się nie\n'
            '  powiodło. Pole "tool" mówi co, "arguments" jak, "tool_result"\n'
            '  dlaczego. Jeżeli "attempt_count" >= 2, agent już skonsultował\n'
            '  CODE_REVIEWERA — sprawdź "code_review". Nie powtarzaj tej samej\n'
            '  komendy. Zmień podejście lub narzędzie.'
        ),
        "TOOL_LIMIT": (
            'TOOL_LIMIT — Gemini wyczerpał limit wywołań narzędzi (zbyt\n'
            '  skomplikowane zadanie). Podziel TASK na mniejsze kroki.'
        ),
        "DONE_REJECTED_VERIFICATION_FAILED": (
            'DONE_REJECTED_VERIFICATION_FAILED — TWOJE poprzednie DONE zostało\n'
            '  odrzucone fizyczną weryfikacją. Pole "checks" mówi CO dokładnie\n'
            '  brakuje. Utwórz TASK który uzupełni KONKRETNIE brakujące dowody.\n'
            '  NIE zwracaj ponownie DONE — poczekaj na kolejny raport.'
        ),
        "TASK_BLOCKED_BY_POLICY": (
            'TASK_BLOCKED_BY_POLICY — zadanie naruszało zakaz pobierania\n'
            '  gotowej gry/APK. Zaproponuj INNE podejście (build od zera).'
        ),
        "TASK_DUPLICATE_OF_VERIFIED_POINT": (
            'TASK_DUPLICATE_OF_VERIFIED_POINT — proponowany TASK jest identyczny\n'
            '  z punktem, który checklist (patrz "PUNKTY ZADAŃ" w kontekście)\n'
            '  już ma jako ZWERYFIKOWANY dowodem z dysku. Pole "message" mówi\n'
            '  którym. NIE ponawiaj go — zaproponuj KOLEJNY, inny krok celu.'
        ),
        "TASK_ALREADY_SATISFIED_ON_DISK": (
            'TASK_ALREADY_SATISFIED_ON_DISK — proponowany TASK sprawdzał coś,\n'
            '  co Python już potwierdził bezpośrednio na dysku (patrz\n'
            '  "message"). Nie twórz go ponownie — zaproponuj NASTĘPNY,\n'
            '  faktycznie jeszcze niezrobiony krok.'
        ),
        "COMPLETED": (
            'COMPLETED — Gemini wykonał blok, pole "report" to jego raport.\n'
            '  NIE oznacza automatycznie DONE całego projektu. Sprawdź raport.'
        ),
    }

    _current_status = (
        last_result.get("status")
        if isinstance(last_result, dict) else None
    )

    _statuses_to_explain = []

    if _current_status in _status_explanations:
        _statuses_to_explain.append(_current_status)

    if "COMPLETED" not in _statuses_to_explain:
        _statuses_to_explain.append("COMPLETED")

    status_interpretation_block = "\n\n".join(
        _status_explanations[s] for s in _statuses_to_explain
    )

    _last_result_is_error = (
        isinstance(last_result, dict)
        and (
            last_result.get("ok") is False
            or last_result.get("status") == "GEMINI_TOOL_ERROR"
        )
    )

    repair_rule_block = (
        """============================================================
ZASADA NAPRAWY PO BŁĘDZIE
============================================================

Jeżeli OSTATNI WYNIK zawiera ok=false lub GEMINI_TOOL_ERROR:

1. NIE zwracaj DONE.
2. Przeczytaj dokładnie: tool, arguments, tool_result, error.
3. Zmień strategię — nie powtarzaj identycznej komendy.
4. Jeżeli błąd to Timeout — zleć tę samą operację przez
   termux_run_background z monitorowaniem procesu.
5. Utwórz konkretny TASK z MIERZALNYM warunkiem sukcesu.
"""
        if _last_result_is_error else ""
    )

    # Ten sam mechanizm co w consult_team()/gemini_execute_task() —
    # zrzuty stanu Chrome/Android tylko wtedy, gdy CEL faktycznie
    # ich dotyczy, zamiast zawsze, niezależnie od typu celu.
    # chrome_text/android_text (opcjonalne) — patrz konsult_team():
    # run_agent() pobiera stan RAZ na krok i przekazuje go tutaj,
    # zamiast MAIN pytającego urządzenie NIEZALEŻNIE o dokładnie to
    # samo, co PLANNER/CRITIC już przed chwilą dostali.
    _resolved_chrome_text = (
        chrome_text if chrome_text is not None else chrome_summary()
    )

    chrome_block = (
        "\nAKTUALNE KARTY CHROME:\n"
        + _resolved_chrome_text
        + "\n"
        if _chrome_relevant_now(goal, last_result) else ""
    )

    android_block = (
        "\nAKTUALNY ANDROID:\n"
        + short(
            android_text if android_text is not None else android_summary(),
            3500
        ) + "\n"
        if _goal_mentions_android(goal) else ""
    )

    # ASK (2026-08-24, na wyraźną prośbę użytkownika — role mają się
    # komunikować "jak człowiek", wołając się po imieniu zamiast
    # sztywnego, zawsze identycznego okrążenia). Zamiast w pełni
    # swobodnego grafu ("każdy pyta każdego" — realne ryzyko pętli
    # i spamu wiadomości do DeepSeeka, które już raz popsuło sesje
    # błędem "invalid message id"), MAIN dostaje JEDNO, ograniczone
    # do RAZY NA KROK prawo zadania KONKRETNEJ roli DODATKOWEGO,
    # celowego pytania — nie zamiennik konsultacji zespołu (którą
    # już dostał wyżej), tylko dopytanie o coś, czego z tamtych
    # odpowiedzi zabrakło. Po odpowiedzi MUSI podjąć decyzję —
    # asked_followup != None sygnalizuje, że limit już wykorzystany.
    if asked_followup:

        ask_block = f"""
============================================================
ODPOWIEDŹ NA TWOJE DODATKOWE PYTANIE
============================================================

Zapytałeś {asked_followup['role']}:
{asked_followup['question']}

Odpowiedź:
{short(str(asked_followup['answer']), 2000)}

Wykorzystałeś już swoje jedno pytanie w tym kroku — TERAZ MUSISZ
zwrócić TASK, DONE albo FAILED. ASK jest w tym wywołaniu
ZABRONIONE.
"""
        ask_contract_block = ""

    else:

        ask_block = ""
        ask_contract_block = f"""
ASK (opcjonalne, NAJWYŻEJ RAZ na krok — nie zamiast konsultacji
zespołu powyżej, tylko dodatkowe, KONKRETNE pytanie do JEDNEJ
roli, gdy z odpowiedzi zespołu powyżej brakuje Ci czegoś
konkretnego do podjęcia decyzji):
{{
  "type": "ASK",
  "ask_role": "jedna z: {", ".join(_MAIN_ASK_ALLOWED_ROLES)}",
  "ask_question": "krótkie, konkretne pytanie — nie ogólne 'co dalej'"
}}
"""

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
INTERPRETACJA STATUSU
============================================================

{status_interpretation_block}

{repair_rule_block}
============================================================

PLANNER:
{team['planner']}

ENGINEER:
{team['engineer']}

RESEARCHER:
{team['researcher']}

CRITIC:
{team['critic']}

BROWSER:
{team['browser']}
{chrome_block}{android_block}
{ask_block}
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
- OBOWIĄZKOWE: jeżeli ENGINEER powyżej podał gotowy
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

NEED_USER_LOGIN (zamiast FAILED, gdy jedynym blokerem jest czynność,
którą fizycznie musi kliknąć/wpisać człowiek — login, kod SMS,
CAPTCHA, zgoda na koncie zewnętrznej usługi — patrz pełny opis w
Twoim prompcie systemowym). "url" TYLKO prawdziwy http(s) adres strony
— jeśli chodzi o Ustawienia Androida/systemu (nie stronę w
przeglądarce), zostaw "url" puste i opisz ścieżkę menu w
"instructions":
{{
  "type": "NEED_USER_LOGIN",
  "reason": "...",
  "url": "... (http(s) albo puste)",
  "instructions": "..."
}}
{ask_contract_block}"""

    return deepseek(
        "MAIN",
        prompt
    )


def _handle_main_ask(
    decision,
    goal,
    step,
    team,
    last_result,
    chrome_text,
    android_text
):
    """
    Obsługuje decyzję MAIN typu ASK (patrz komentarz przy
    ask_contract_block w main_decide()) — wysyła pytanie do
    JEDNEJ, konkretnej roli, wraca z odpowiedzią do MAIN i wymusza
    kolejną decyzję (z asked_followup ustawionym — ASK jest w niej
    zablokowane, patrz main_decide()).

    Zwraca sparsowaną, ostateczną decyzję (TASK/DONE/FAILED) albo
    None, gdy pytanie było nieprawidłowe (zła rola / puste pytanie)
    albo MAIN spróbował zapytać DRUGI raz w tym samym kroku mimo
    limitu — w obu przypadkach wywołujący potraktuje to dokładnie
    tak samo jak niepoprawny JSON (istniejący mechanizm naprawy).
    """

    ask_role = str(decision.get("ask_role", "")).strip().upper()
    ask_question = str(decision.get("ask_question", "")).strip()

    if ask_role not in _MAIN_ASK_ALLOWED_ROLES or not ask_question:

        log(
            "MAIN",
            "ASK odrzucone (nieprawidłowa rola '" + ask_role
            + "' lub puste pytanie) — traktuję jak brak decyzji."
        )

        return None

    log(
        "MAIN",
        "ASK -> " + ask_role + ": " + short(ask_question, 300)
    )

    answer = deepseek(
        ask_role,
        "MAIN ma do Ciebie DODATKOWE, KONKRETNE pytanie — nie "
        "kolejną pełną konsultację, tylko jedną, precyzyjną rzecz "
        "do doprecyzowania. Odpowiedz krótko i wyłącznie na nie:\n\n"
        + ask_question
    )

    log(
        "MAIN",
        ask_role + " ODPOWIEDŹ NA PYTANIE MAIN: "
        + short(str(answer), 300)
    )

    raw = main_decide(
        goal,
        step,
        team,
        last_result,
        chrome_text,
        android_text,
        asked_followup={
            "role": ask_role,
            "question": ask_question,
            "answer": answer
        }
    )

    decision = parse_json(raw)

    if isinstance(decision, dict) and decision.get("type") == "ASK":

        log(
            "MAIN",
            "ASK ponownie mimo wykorzystanego limitu 1/krok — "
            "ignoruję, wymuszam standardową naprawę JSON."
        )

        return None

    return decision


# ============================================================
# PARSE JSON
# ============================================================

def _extract_last_balanced_json_object(text):
    """
    Znajduje OSTATNI, poprawnie zbalansowany obiekt JSON {...},
    skanując WSTECZ od końca tekstu i licząc głębokość nawiasów.

    Zaobserwowany realny problem: DeepSeek czasem poprzedza właściwą
    odpowiedź długim fragmentem własnego rozumowania (po angielsku,
    zanim przejdzie do krótkiej, właściwej odpowiedzi) — a taki
    fragment potrafi zawierać przykładowe '{'/'}' (np. tłumacząc
    format JSON albo cytując fragment kodu). Naiwne dopasowanie
    "pierwszy '{' w całym tekście .. ostatni '}'" łapało wtedy
    niepoprawną, za szeroką parę nawiasów i psuło parsowanie mimo że
    właściwy, poprawny obiekt JSON był obecny na końcu tekstu — stąd
    powtarzające się w logach "Niepoprawny JSON. Naprawiam." Szukanie
    WSTECZ od ostatniego '}' poprawnie trafia na OSTATNI kompletny
    obiekt, niezależnie od tego, co poprzedza go w tekście.
    """

    depth = 0
    end = None
    start = None

    for i in range(len(text) - 1, -1, -1):

        ch = text[i]

        if ch == "}":
            if end is None:
                end = i
            depth += 1

        elif ch == "{":
            depth -= 1
            if depth == 0 and end is not None:
                start = i
                break

    if start is None or end is None:
        return None

    try:
        obj = json.loads(text[start:end + 1])
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    return None


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

    obj = _extract_last_balanced_json_object(text)

    if obj is not None:
        return obj

    # Ostatnia deska ratunku: powyższe próby wyciągają JSON z
    # otoczenia (tekst dookoła, blok ```json```), ale żadna z nich nie
    # naprawia SKŁADNIOWO zepsutego JSON-a (brakujący cudzysłów,
    # przecinek na końcu listy, niedomknięty nawias) — a to zdarza
    # się realnie, bo opendeep steruje stroną czatu, nie oficjalnym
    # API, więc formatowanie bywa mniej stabilne. Jeśli json_repair
    # nie jest zainstalowany, _repair_json jest None i ten blok jest
    # po prostu pomijany — zachowanie identyczne jak wcześniej.
    if _repair_json is not None:

        try:

            repaired = _repair_json(text, return_objects=True)

            if isinstance(repaired, dict):
                return repaired

            if isinstance(repaired, str):

                obj = json.loads(repaired)

                if isinstance(obj, dict):
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


def record_generic_tool_failure_streak(tool):
    """
    Zlicza porażki Z RZĘDU TEGO SAMEGO narzędzia, niezależnie od
    dokładnych argumentów — w odróżnieniu od record_tool_attempt()
    (który wymaga IDENTYCZNYCH argumentów, więc nigdy nie złapie
    zespołu próbującego kolejnych, coraz to innych wariantów tej
    samej w gruncie rzeczy czynności, np. `grep`/`strings` z innym
    wzorcem za każdym razem na tym samym pliku).
    """

    data = read_json(TOOL_FAILURE_STREAK_FILE, {})

    if data.get("tool") == tool:
        streak = data.get("streak", 0) + 1
    else:
        streak = 1

    write_json(TOOL_FAILURE_STREAK_FILE, {"tool": tool, "streak": streak})

    return streak


def reset_generic_tool_failure_streak():
    """
    Zeruje serię porażek z rzędu — wywoływane po KAŻDYM udanym
    zakończeniu TASK-u (dowolnym narzędziem), bo seria dotyczy
    kolejnych, nieprzerwanych porażek, nie porażek w ogóle.
    """

    write_json(TOOL_FAILURE_STREAK_FILE, {"tool": None, "streak": 0})


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
            + shlex.quote(str(apk_path))
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


_APK_NEGATION_WINDOW_CHARS = 30
_APK_NEGATION_WORDS = {"nie", "bez", "ani", "not", "no"}


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

    Zaobserwowany realny problem: goły keyword-match nie rozumie
    przeczenia. Cel wprost piszący "To NIE jest gra ani aplikacja do
    zbudowania, więc nie wymagaj pliku .apk" nadal zawiera słowa
    "gra" i "apk" — pierwsza wersja tej funkcji uznawała to za
    wymóg APK, DOKŁADNIE ODWROTNIE niż jawna treść celu. Skutek w
    logu: MAIN poprawnie zdiagnozował błąd systemu weryfikacji, ale
    zespół zamiast czekać na poprawkę, sam sobie "naprawił" problem
    tworząc pusty, fałszywy `dummy.apk` tylko po to, żeby przejść
    check — nie tego chcemy. Teraz dla KAŻDEGO trafienia słowa
    kluczowego sprawdzamy kilka poprzedzających je słów pod kątem
    przeczenia ("nie", "bez", "ani") — jeśli występuje, to trafienie
    nie liczy się jako wymóg APK. Cel wymaga APK tylko, jeśli
    istnieje przynajmniej JEDNO trafienie BEZ przeczenia w pobliżu.
    """

    if not goal:
        return False

    text = str(goal)

    for match in _APK_GOAL_PATTERN.finditer(text):

        window_start = max(0, match.start() - _APK_NEGATION_WINDOW_CHARS)
        preceding_words = re.findall(
            r"\w+",
            text[window_start:match.start()].lower()
        )

        if any(
            w in _APK_NEGATION_WORDS
            for w in preceding_words[-4:]
        ):
            continue

        return True

    return False


# Ścieżki w stylu "~/coś.rozszerzenie" wspomniane wprost w treści
# celu (np. "zapisz plik ~/raport_systemowy.txt").
_GOAL_FILE_PATH_PATTERN = re.compile(r"~[\w./\-]+\.\w+")

# Granica "klauzuli" wokół ścieżki pliku, w której szukamy sygnału
# "ten plik ma CELOWO nie istnieć" — patrz _extract_goal_mentioned_files.
# Zatrzymujemy się na przecinku/średniku/nowej linii, żeby sygnał przy
# JEDNYM pliku w zdaniu nie "przeciekał" na SĄSIEDNI plik wymieniony
# w tym samym zdaniu, ale w innej klauzuli (np. "Napisz skrypt
# (~/a.py), ktory CELOWO czyta ~/b.txt" — sygnał dotyczy tylko b.txt).
_GOAL_FILE_CLAUSE_BOUNDARY_PATTERN = re.compile(r"[,;\n]")

_GOAL_FILE_NONEXISTENCE_MARKERS = (
    "celowo", "nieistnieje", "nie istnieje", "oczekiwany błąd",
    "oczekiwany blad", "spodziewany błąd", "spodziewany blad",
    "wynik to błąd", "wynik to blad", "intentionally", "does not exist",
    "doesn't exist", "not exist",
)


def _extract_goal_mentioned_files(goal):
    """
    Wyciąga z treści CELU/warunku sukcesu ścieżki plików wprost
    wymienionych (np. "~/raport_systemowy.txt"). To pozwala sprawdzić
    NIEZALEŻNIE OD TEGO, CO DEKLARUJE GEMINI/MAIN — bezpośrednio na
    dysku — czy te konkretne pliki faktycznie istnieją i nie są
    puste, zamiast ufać samej prozie w raporcie ("plik zapisany",
    "zrzut zrobiony").

    Zaobserwowany realny problem: raport potrafi z pewnością siebie
    twierdzić, że coś jest "potwierdzone", z konkretnymi
    (czasem zmyślonymi) liczbami, bez żadnego świeżego sprawdzenia.
    Ten mechanizm nie ocenia TREŚCI (na to nie ma prostego, w pełni
    niezawodnego sposobu bez kolejnego modelu), ale przynajmniej
    twardo sprawdza, że deklarowane pliki NAPRAWDĘ ISTNIEJĄ i mają
    jakąkolwiek zawartość — najbardziej podstawowy, ale całkowicie
    niezależny od LLM fakt do zweryfikowania.

    Zaobserwowany w produkcji realny problem (log v73): cel opisywał
    krok testujący obsługę błędu — "spróbuj CELOWO odczytać plik
    ~/nieistnieje_v73.txt — oczekiwany wynik to błąd (CELOWE)". Ten
    plik ma z definicji NIGDY nie powstać. Pierwsza wersja tej
    funkcji łapała KAŻDĄ ścieżkę pasującą do wzorca "~/coś.rozsz",
    więc uznawała ten celowo nieistniejący plik za wymagany dowód —
    check "Pliki wymienione w treści CELU" był wtedy niemożliwy do
    spełnienia w ogóle, a DONE odrzucane w nieskończoność mimo
    faktycznego ukończenia całego celu (zespół DeepSeek w tym czasie,
    nie znając prawdziwej przyczyny, sam zmyślał niepowiązane
    diagnozy typu "plik raportu jest ucięty w połowie zdania").
    Ta sama funkcja jest też używana dla success_condition
    pojedynczego TASKa (patrz _verify_success_condition_evidence),
    gdzie pliki zwykle NIE są poprzedzone czasownikiem zapisu (np.
    "Plik ~/wynik.txt istnieje i ma treść 'OK'") — dlatego zamiast
    wymagać jakiegoś konkretnego czasownika w pobliżu (co złamałoby
    ten drugi przypadek), wycinamy tylko te ścieżki, przy których
    tekst WPROST sygnalizuje, że mają CELOWO nie istnieć (patrz
    _GOAL_FILE_NONEXISTENCE_MARKERS) — domyślnie plik nadal jest
    wymagany, wykluczamy go tylko przy jawnym sygnale przeciwnym.
    """

    if not goal:
        return []

    text = str(goal)
    text_lower = text.lower()
    seen = []

    for match in _GOAL_FILE_PATH_PATTERN.finditer(text):

        boundary_before = None
        for boundary_match in _GOAL_FILE_CLAUSE_BOUNDARY_PATTERN.finditer(
            text, 0, match.start()
        ):
            boundary_before = boundary_match.end()
        window_start = boundary_before if boundary_before is not None else 0

        boundary_after = _GOAL_FILE_CLAUSE_BOUNDARY_PATTERN.search(
            text, match.end()
        )
        window_end = boundary_after.start() if boundary_after else len(text)

        window = text_lower[window_start:window_end]

        expected_to_not_exist = any(
            marker in window
            for marker in _GOAL_FILE_NONEXISTENCE_MARKERS
        )

        if expected_to_not_exist:
            continue

        if match.group(0) not in seen:
            seen.append(match.group(0))

    return seen


# Wyciąga konkretną treść FINAL_OK.txt, jeśli CEL ją wprost cytuje
# (np. `utworz plik FINAL_OK.txt z trescia "TEST_ZAKONCZONY"`) —
# pozwala zweryfikować DOKŁADNIE to, co użytkownik ustalił w CELU,
# zamiast jakiegoś nieznanego zespołowi, hardcoded tokenu.
_FINAL_OK_CONTENT_PATTERN = re.compile(
    r"FINAL_OK\.txt[^\"'\n]{0,40}[\"']([^\"'\n]{1,200})[\"']",
    re.IGNORECASE
)


def _head_tail_preview(text, head_chars=150, tail_chars=150):
    """
    Podgląd treści pliku dla _goal_progress_snapshot() — POCZĄTEK i
    KONIEC, nie tylko początek, z jawnym oznaczeniem, że coś zostało
    pominięte.

    Zaobserwowany realny problem (log 2026-08-24, test "uniwersalny",
    KROK 4-7): poprzednia wersja pokazywała WYŁĄCZNIE pierwsze 80
    znaków pliku, bez żadnego oznaczenia obcięcia. Punkt 1 zapisywał
    do tego samego pliku długi zrzut `ls -la` jako PIERWSZą rzecz, a
    punkt 6 (zdanie o stronie z RESEARCHERA) było DOPISywane na
    KONIEC tego samego pliku w kolejnym kroku — podgląd 80 znaków od
    początku nigdy nie sięgał do tego zdania. Efekt: CRITIC widział
    tylko "total 7902\ndrwx------...", nigdy dowodu na punkt 6, i
    blokował TEN SAM krok 4 razy z rzędu jako "brak rzeczywistego
    wywołania RESEARCHERA", mimo że fizycznie było już zrobione —
    zespół stał w miejscu, paląc wiadomości DeepSeeka na coś, co już
    było gotowe. Pliki uzupełniane kolejnymi TASKami (`>>` w shellu)
    mają najświeższy dowód na KOŃCU, więc pokazujemy oba krańce.
    """

    text = text.replace("\n", " ")

    if len(text) <= head_chars + tail_chars:
        return "\"" + text + "\""

    omitted = len(text) - head_chars - tail_chars

    return (
        "\"" + text[:head_chars] + "\" ... (pominięto " + str(omitted)
        + " znaków w środku) ... \"" + text[-tail_chars:] + "\""
    )


def _goal_progress_snapshot(goal):
    """
    Tani, LOKALNY (zero wywołań LLM) przegląd stanu faktycznego celu
    — bezpośrednio z dysku, nie z pamięci rozmowy. Zaobserwowany
    realny problem: zespół "gubi się" między krokami — powtarza już
    zrobione rzeczy (v40) albo nie zauważa, że coś już istnieje,
    mimo że PLANNER/CRITIC muszą to wywnioskować wyłącznie z historii
    rozmowy, która przy długiej sesji robi się bardzo długa. Zamiast
    wydłużać prompt o więcej historii (dokładnie czego NIE chcemy —
    sekwencje mają zostać krótkie), dajemy im nowy, zawsze świeży,
    kilkulinijkowy stan faktyczny na podstawie tych samych sprawdzeń,
    których używa verify_final() — więc to, co widzą, jest DOKŁADNIE
    tym, co realnie zadecyduje o DONE/FAILED, a nie osobną, mogącą się
    rozjechać wersją prawdy.
    """

    lines = []

    for rel_path in _extract_goal_mentioned_files(goal)[:6]:

        try:
            p = Path(rel_path).expanduser()
        except Exception:
            continue

        if not p.exists():
            lines.append("- " + rel_path + ": BRAK")
            continue

        try:
            size = p.stat().st_size
        except Exception:
            size = 0

        if size == 0:
            lines.append("- " + rel_path + ": istnieje, ale PUSTY (0 B)")
            continue

        preview = ""

        try:
            preview = _head_tail_preview(
                p.read_text(encoding="utf-8", errors="replace")
            )
        except Exception:
            pass

        lines.append(
            "- " + rel_path + ": istnieje (" + str(size) + " B)"
            + (" — " + preview if preview else "")
        )

    # UWAGA (zaobserwowany realny bug): ta pętla dawniej NIE
    # sprawdzała świeżości pliku względem BIEŻĄCEGO celu — mimo że
    # docstring tej funkcji obiecuje "te same sprawdzenia, których
    # używa verify_final()", a verify_final() takie sprawdzenie MA
    # (patrz komentarz przy checks.append FINAL_OK.txt). Efekt:
    # stary FINAL_OK.txt sprzed zupełnie innego, wcześniejszego celu
    # (np. ~/FINAL_OK.txt z treścią "TEST_V73_ZAKONCZONY" sprzed
    # wielu dni) był pokazywany zespołowi jako aktualny stan, co
    # realnie zmyliło PLANNERA ("FINAL_OK.txt potwierdza jedynie
    # poprzedni test V73"). Naprawiono przez dodanie identycznego
    # progu mtime < goal_started_at co w verify_final().

    try:
        goal_started_at = GOAL_FILE.stat().st_mtime
    except Exception:
        goal_started_at = 0.0

    final_ok_seen = False
    final_ok_stale_seen = False

    for candidate in (
        HOME / "FINAL_OK.txt",
        AGENT_DIR / "FINAL_OK.txt",
        APK_OUTPUT_DIR / "FINAL_OK.txt"
    ):

        content = read_text(candidate).strip()

        if not content:
            continue

        try:
            is_stale = candidate.stat().st_mtime < goal_started_at
        except Exception:
            is_stale = False

        if is_stale:
            final_ok_stale_seen = True
            continue

        lines.append(
            "- FINAL_OK.txt: istnieje (" + str(candidate) + ") — \""
            + content[:60] + "\""
        )
        final_ok_seen = True
        break

    if not final_ok_seen:
        if final_ok_stale_seen:
            lines.append(
                "- FINAL_OK.txt: BRAK dla BIEŻĄCEGO celu (znaleziono "
                "tylko plik STARSZY niż ten cel — to pozostałość po "
                "wcześniejszym, niepowiązanym zadaniu, zignoruj jego "
                "treść)"
            )
        else:
            lines.append("- FINAL_OK.txt: BRAK (nigdzie nie znaleziono)")

    if CUSTOM_TOOLS:

        # Zaobserwowany realny problem (ten sam log co _head_tail_
        # preview): custom_tools/ CELOWO przetrwa między celami (to
        # trwałe narzędzia, nie dane jednorazowe — patrz komentarz
        # przy cleanupie). Gdy CEL każe "stwórz NOWE narzędzie X", a
        # X akurat istnieje z zupełnie INNEGO, wcześniejszego celu,
        # sama nazwa w tej liście nie mówi zespołowi, czy to
        # faktycznie świeże, czy stary, niepowiązany plik — PLANNER
        # rozsądnie chciał go użyć, CRITIC równie rozsądnie się nie
        # zgadzał (bo cel dosłownie mówił "nowe"), i krok wracał w
        # kółko. Znacznik "z tego celu"/"sprzed tego celu" (po dacie
        # modyfikacji pliku narzędzia względem startu BIEŻĄCEGO celu
        # — ten sam mechanizm co świeżość FINAL_OK.txt w
        # verify_final()) daje obu rolom tę samą, jednoznaczną
        # odpowiedź zamiast zgadywania.
        try:
            goal_started_at = GOAL_FILE.stat().st_mtime
        except Exception:
            goal_started_at = 0.0

        tool_notes = []

        for tool_name in sorted(CUSTOM_TOOLS.keys()):

            source_file = CUSTOM_TOOLS[tool_name].get("source_file")

            try:
                is_fresh = (
                    source_file
                    and Path(source_file).stat().st_mtime
                    >= goal_started_at
                )
            except Exception:
                is_fresh = None

            if is_fresh is True:
                tool_notes.append(tool_name + " (z tego celu)")
            elif is_fresh is False:
                tool_notes.append(tool_name + " (sprzed tego celu)")
            else:
                tool_notes.append(tool_name)

        lines.append(
            "- Zarejestrowane custom_tools: " + ", ".join(tool_notes)
        )

    if not lines:
        return ""

    return (
        "STAN FAKTYCZNY (sprawdzony TERAZ bezpośrednio na dysku, "
        "nie z pamięci rozmowy — traktuj jako pewnik):\n"
        + "\n".join(lines)
    )


def _expected_final_ok_content(goal):

    if not goal:
        return None

    match = _FINAL_OK_CONTENT_PATTERN.search(str(goal))

    if not match:
        return None

    return match.group(1).strip()


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
    #
    # Zaobserwowany realny, poważny bug: ten check wymagał treści
    # DOKŁADNIE równej hardcoded FINAL_OK_TOKEN ("ANDROID_GAME_
    # BUILD_OK") — stałej z czasów, gdy agent budował WYŁĄCZNIE gry
    # Android. Ten token NIGDY nie jest komunikowany zespołowi w
    # żadnym prompcie (MAIN_PROMPT każe użyć treści "ustalonej z
    # użytkownikiem", czyli tego, co faktycznie mówi CEL — np.
    # "TEST_ZAKONCZONY"), więc żaden uniwersalny (nie-growy) cel nie
    # mógł go NAPRAWDĘ spełnić. A mimo to DONE bywało akceptowane —
    # bo GDZIEŚ z dawnej, historycznej sesji budowania gry zostawał
    # stary, POPRAWNY co do tokenu plik w APK_OUTPUT_DIR, który po
    # cichu satysfakcjonował TEN check dla zupełnie NIEPOWIĄZANEGO,
    # późniejszego celu — podczas gdy prawdziwy FINAL_OK.txt tego
    # celu (zwykle pod ~/FINAL_OK.txt, ścieżka spoza dotychczasowych
    # kandydatów) był całkowicie ignorowany. Potwierdzone
    # eksperymentalnie: DONE_ok=True nawet gdy jedynym pasującym
    # plikiem był ten stary, niepowiązany z bieżącym celem.
    #
    # Naprawiono: dodano ~/FINAL_OK.txt (realnie używana ścieżka) do
    # kandydatów; wymagamy TREŚCI wskazanej wprost w CELU (jeśli
    # CEL cytuje konkretny napis przy "FINAL_OK.txt", np. w
    # cudzysłowie) albo dowolnej niepustej treści, gdy CEL tego nie
    # precyzuje; i — kluczowe — plik musi być ŚWIEŻY (zapisany PO
    # rozpoczęciu BIEŻĄCEGO celu, nie starszy niż GOAL_FILE), żeby
    # stary plik z zupełnie innego, wcześniejszego celu nie mógł już
    # nigdy po cichu "pożyczyć" swojej ważności nowemu zadaniu.

    expected_final_ok_content = _expected_final_ok_content(goal)

    final_ok_candidates = [
        HOME / "FINAL_OK.txt",
        AGENT_DIR / "FINAL_OK.txt",
        APK_OUTPUT_DIR / "FINAL_OK.txt",
    ]

    try:
        goal_started_at = GOAL_FILE.stat().st_mtime
    except Exception:
        goal_started_at = 0.0

    final_ok_found = False
    final_ok_path = None
    final_ok_reject_reason = "Nie znaleziono pliku FINAL_OK.txt."

    for candidate in final_ok_candidates:

        if not candidate.exists():
            continue

        content = read_text(candidate).strip()

        if not content:
            final_ok_reject_reason = str(candidate) + " istnieje, ale jest pusty."
            continue

        try:
            is_stale = candidate.stat().st_mtime < goal_started_at
        except Exception:
            is_stale = False

        if is_stale:
            final_ok_reject_reason = (
                str(candidate) + " istnieje, ale jest STARSZY niż "
                "bieżący cel — to prawdopodobnie pozostałość po "
                "wcześniejszym, niepowiązanym zadaniu, nie dowód "
                "ukończenia TEGO celu."
            )
            continue

        if (
            expected_final_ok_content is not None
            and content != expected_final_ok_content
        ):
            final_ok_reject_reason = (
                str(candidate) + " istnieje, ale treść ('" + content
                + "') nie zgadza się z tą wskazaną w CELU ('"
                + expected_final_ok_content + "')."
            )
            continue

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
            else final_ok_reject_reason
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
                "unzip -l " + shlex.quote(str(apk_path)),
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
            "adb shell pm list packages | grep " + shlex.quote(package_name),
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

    # --- 4. Pliki wprost wymienione w treści CELU ------------------
    #
    # Sprawdzane BEZPOŚREDNIO na dysku, niezależnie od tego, co
    # deklaruje raport Gemini/MAIN — jeśli cel mówi "zapisz plik
    # ~/raport_systemowy.txt", ten plik MUSI faktycznie istnieć i
    # być niepusty, niezależnie od tego, jak pewnie brzmi raport.

    mentioned_files = _extract_goal_mentioned_files(goal)

    if mentioned_files:

        missing_or_empty = []

        for rel_path in mentioned_files:

            p = Path(rel_path).expanduser()

            if not p.exists() or p.stat().st_size == 0:
                missing_or_empty.append(rel_path)

        checks.append({
            "check": "Pliki wymienione w treści CELU",
            "required": True,
            "ok": not missing_or_empty,
            "detail": (
                "Wszystkie wymienione w celu pliki istnieją i są "
                "niepuste: " + ", ".join(mentioned_files)
                if not missing_or_empty
                else "BRAKUJE lub są PUSTE (sprawdzone bezpośrednio "
                "na dysku, niezależnie od raportu): "
                + ", ".join(missing_or_empty)
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


def _looks_like_failure_report(text):
    """
    Prosty, celowo niewyczerpujący heurystyczny detektor: czy wpisany
    przez użytkownika tekst brzmi jak ZGŁOSZENIE PROBLEMU/BŁĘDU
    (np. "ta strona się nie wczytuje", "nie zalogowałem się"), a nie
    jak potwierdzenie sukcesu ani przekazana wartość (klucz/kod).

    Zaobserwowany realny bug (log 2026-08-27): użytkownik napisał
    "https://app.vapi.ai/login ta strona sie nie wczytuje" w
    odpowiedzi na prośbę o zalogowanie — _handle_need_user_login()
    mimo to zawsze budował note z twierdzeniem, że logowanie zostało
    ręcznie potwierdzone. Fałszywie ujemne dopasowanie (heurystyka
    nie złapie zgłoszenia problemu) nie jest tragiczne — zespół i tak
    dostaje neutralną notatkę każącą mu samemu ocenić treść, zamiast
    z góry zakładać sukces jak poprzednio.
    """

    lowered = text.lower()

    failure_markers = (
        "nie wczytuje", "nie wczyt", "nie ładuje", "nie laduje",
        "nie działa", "nie dziala", "błąd", "blad", "error",
        "nie mogę", "nie moge", "nie udało", "nie udalo",
        "nie otwiera", "nie otworz", "problem", "zawiesza",
        "wywala", "nie widzę", "nie widze", "nie mam dostępu",
        "nie mam dostepu", "nie zalogow", "nie działała",
        "nie dzialala",
    )

    return any(marker in lowered for marker in failure_markers)


# Zaobserwowany realny wzorzec (log 2026-08-28): agent CZASEM sam
# znajduje dane kontaktowe (numer telefonu) na telefonie przez
# `termux-contact-list`, zanim o nie zapyta (patrz MAIN_PROMPT,
# sekcja "Create Assistant"/kontakty, v135) — ale to WYŁĄCZNIE
# instrukcja w prompcie, więc bywa pomijana: w jednej sesji zadziałała,
# w kolejnej, bardzo podobnej, MAIN od razu zapytał użytkownika o numer
# Beaty, mimo że nigdy nie spróbował kontaktów. Użytkownik poprosił o
# TWARDY (deterministyczny) mechanizm w Pythonie — ale WYRAŹNIE
# zastrzegł, żeby nie zabić naturalnej, elastycznej rozmowy zespołu.
# Dlatego to NIE jest twardy błąd/wyjątek: to ten sam, już istniejący
# mechanizm "miękkiego przekierowania" co
# TASK_DUPLICATE_OF_VERIFIED_POINT/TASK_ALREADY_SATISFIED_ON_DISK —
# ustawia last_result i `continue`, więc trafia do Oli (tłumaczenie na
# ludzki język, v133) i wraca do MAIN jako zwykła, naturalna kolejna
# tura rozmowy, nie awaria. Zabezpieczenie przed zapętleniem: po
# _CONTACT_GATE_MAX_REDIRECTS przekierowaniach w TEJ SAMEJ sesji agent
# przestaje blokować (np. gdy uprawnienie do kontaktów jest odmówione
# i sprawdzanie nigdy się nie powiedzie) — nie chcemy nieskończonej
# pętli w imię "bezpieczeństwa".
_CONTACT_GATE_MAX_REDIRECTS = 2

_CONTACT_INFO_REQUEST_RE = re.compile(
    r"numer\s+telefonu|numer\s+kontaktow|numer\s+do\b|telefon\s+do\b|"
    r"phone\s+number|contact\s+number",
    re.IGNORECASE
)


def _decision_asks_for_contact_info(decision):

    # Prawdziwy adres http(s) oznacza prośbę o logowanie na stronie,
    # nie o dane kontaktowe — celowo poza zakresem tej bramki.
    if str(decision.get("url", "")).strip():
        return False

    text = (
        str(decision.get("reason", ""))
        + " "
        + str(decision.get("instructions", ""))
    )

    return bool(_CONTACT_INFO_REQUEST_RE.search(text))


def _mark_contacts_lookup_attempted():

    try:
        CONTACTS_LOOKUP_ATTEMPTED_FILE.parent.mkdir(
            parents=True, exist_ok=True
        )
        CONTACTS_LOOKUP_ATTEMPTED_FILE.write_text("1")
    except Exception:
        pass


def _contacts_lookup_attempted():

    return CONTACTS_LOOKUP_ATTEMPTED_FILE.exists()


def _need_user_login_with_contact_gate(decision, contact_gate_redirects):
    """
    Wspólna logika dla OBU miejsc w run_agent(), w których obsługiwany
    jest NEED_USER_LOGIN (główna decyzja MAIN ORAZ alternatywa po
    FAILED) — patrz też docstring _handle_need_user_login() o tym
    samym ryzyku rozjazdu między dwiema ścieżkami. Owija
    _handle_need_user_login() bramką kontaktów (patrz komentarz przy
    _decision_asks_for_contact_info() wyżej). Zwraca
    (last_result, nowy_licznik_przekierowań) — wywołujący musi
    nadpisać swoją lokalną zmienną licznika wynikiem.
    """

    if (
        _decision_asks_for_contact_info(decision)
        and not _contacts_lookup_attempted()
        and contact_gate_redirects < _CONTACT_GATE_MAX_REDIRECTS
    ):

        contact_gate_redirects += 1

        log(
            "MAIN",
            "NEED_USER_LOGIN o dane kontaktowe odrzucone -- "
            "kontakty na telefonie nie były jeszcze sprawdzone w tej "
            "sesji (" + str(contact_gate_redirects) + "/"
            + str(_CONTACT_GATE_MAX_REDIRECTS) + ")."
        )

        return (
            {
                "status": "TRY_CONTACTS_FIRST",
                "message": (
                    "Zanim poprosisz użytkownika o numer/kontakt, "
                    "sprawdź najpierw kontakty zapisane na telefonie "
                    "(termux-contact-list) — ta informacja może już "
                    "tam być, tak jak wcześniej w tej samej rozmowie. "
                    "Jeśli po faktycznym sprawdzeniu kontaktów nadal "
                    "jej brakuje (albo brak uprawnienia), dopiero "
                    "wtedy poproś użytkownika."
                )
            },
            contact_gate_redirects
        )

    return (_handle_need_user_login(decision), contact_gate_redirects)


def _handle_need_user_login(decision):
    """
    Obsługa decyzji NEED_USER_LOGIN — wspólna dla dwóch miejsc w
    run_agent(): gdy to GŁÓWNA decyzja MAIN w danym kroku, ORAZ gdy
    to ALTERNATYWA, którą MAIN podaje po własnym FAILED (patrz
    "Sprawdź jeszcze raz" w run_agent()).

    Zaobserwowany realny bug (log 2026-08-26): druga ścieżka
    (alternatywa po FAILED) sprawdzała WYŁĄCZNIE, czy
    alt.get("type") == "TASK" — gdy MAIN sam podał tam
    NEED_USER_LOGIN (np. "wygeneruj klucz API ręcznie"), kod po
    cichu to ignorował i kończył sesję zwykłym FAILED, mimo że MAIN
    dosłownie właśnie podał wykonalną drogę do przodu. Wydzielenie
    tej funkcji gwarantuje, że obie ścieżki obsługują NEED_USER_LOGIN
    identycznie, zamiast ryzykować rozjazd przy przyszłych zmianach.

    Otwiera podaną stronę w Chrome (TYLKO prawdziwe http(s), patrz
    komentarz niżej), czeka na terminalu na potwierdzenie
    użytkownika, i zwraca last_result do ustawienia przez wywołującego
    (który MUSI potem zrobić `continue`, nie `return`).
    """

    login_reason = str(decision.get("reason", ""))
    login_url = str(decision.get("url", "")).strip()
    login_instructions = str(decision.get("instructions", ""))

    print()
    print("=" * 72)
    print("AGENT POTRZEBUJE TWOJEGO DZIAŁANIA W PRZEGLĄDARCE")
    print("=" * 72)

    if login_reason:
        print("Powód: " + login_reason)

    if login_url:
        print("Strona: " + login_url)

    if login_instructions:
        print("Co zrobić: " + login_instructions)

    # Zaobserwowany realny problem (log 2026-08-26): MAIN podał "url":
    # "android://settings/apps/com.termux/permissions" dla kroku,
    # który w ogóle nie dotyczył strony w przeglądarce, tylko
    # systemowych Ustawień Androida. chrome_open() na taki nie-http(s)
    # URI po cichu "udawał sukces" (fallback przez `am start` zwracał
    # kod 0), mimo że Chrome faktycznie zostawał na pustej nowej
    # karcie. Otwieramy w Chrome TYLKO prawdziwe http(s) adresy; w
    # przeciwnym razie polegamy wyłącznie na czytelnym tekście w
    # "instructions" (który i tak MAIN już podaje).
    if login_url.startswith(("http://", "https://")):

        open_result = chrome_open(login_url)

        if not open_result.get("ok"):
            print(
                "(nie udało się automatycznie otworzyć strony "
                "— otwórz ją ręcznie w Chrome: " + login_url + ")"
            )

    elif login_url:

        print(
            "(to nie jest adres strony internetowej — "
            "wykonaj czynność ręcznie, tak jak opisano wyżej "
            "w \"Co zrobić\")"
        )

    # Zaobserwowany realny bug (log 2026-08-27): użytkownik, zapytany
    # przez MAIN o np. klucz API, w NATURALNY sposób wklejał go
    # BEZPOŚREDNIO w to okienko ("masz tu api key : org_...") zamiast
    # otwierać drugą sesję Termux i ręcznie zapisywać go do pliku,
    # którego ścieżkę MAIN podał w wolnym tekście "instructions". Kod
    # jednak używał input() WYŁĄCZNIE jako blokady do naciśnięcia
    # Enter — cokolwiek użytkownik faktycznie wpisał, było CAŁKOWICIE
    # WYRZUCANE. Efekt: "podaję klucz, a agent nic z nim nie robi".
    # Naprawiono: to, co użytkownik wpisał, trafia teraz do last_result
    # i zespół (PLANNER/ENGINEER) może użyć tego wprost w następnym
    # TASKu, zamiast zakładać że dane leżą już w jakimś pliku.
    # UWAGA: MUSI być _read_full_input(), nie zwykłe input() — patrz
    # jego docstring. Zaobserwowany realny bug (log 2026-08-27, cel:
    # Auth Token Twilio): użytkownik wklejał WIELOLINIOWY fragment
    # strony (np. "Live credentials\nAccount SID...\nAuth token\n...
    # \n93a29e032a..."). Zwykłe input() oddaje TYLKO pierwszą linię
    # ("Live credentials", ok. 16 znaków — dokładnie tyle, ile log
    # pokazał jako długość wklejonej wartości) — cała reszta,
    # WŁĄCZNIE Z PRAWDZIWYM TOKENEM na końcu, zostawała w buforze
    # stdin i była po cichu "konsumowana" przez KOLEJNE input() całe
    # kroki później (stąd późniejsze, pozornie bezsensowne wklejki typu
    # "Auth token" — to fragment TEJ SAMEJ, dawno wklejonej treści), a
    # ostatecznie resztki lądowały jako dosłowne (błędne) polecenia w
    # powłoce Termux po zakończeniu programu. To dokładnie ten sam
    # mechanizm, który wcześniej naprawiono dla pola CELU.
    user_typed = _read_full_input(
        "Gdy skończysz (zalogowano/potwierdzono), wciśnij Enter, żeby "
        "agent kontynuował — albo, jeśli masz gotową wartość (np. "
        "klucz/kod/numer) do przekazania, wklej ją tutaj i wciśnij "
        "Enter > "
    ).strip()

    # Zaobserwowany realny bug (log 2026-08-27, cel: telefon do
    # Beaty): kod NIŻEJ wcześniej ZAWSZE budował last_result ze
    # "status": "USER_LOGIN_COMPLETED" i notatką "Użytkownik
    # potwierdził, że ręcznie dokończył...", NIEZALEŻNIE od tego, co
    # użytkownik faktycznie wpisał. Użytkownik napisał wprost
    # "https://app.vapi.ai/login ta strona sie nie wczytuje" — czyli
    # ZGŁASZAŁ, że strona logowania się NIE otworzyła (nie zalogował
    # się) — a Ola/BROWSER (tłumacząca ten raport na ludzki język,
    # patrz consult_team()) i tak przetłumaczyła to jako "użytkownik
    # potwierdził logowanie", co PLANNER wziął za fakt ("użytkownik
    # jest już zalogowany, potwierdzone ręcznie") i zbudował na tym
    # kolejny krok. To była czysta, z góry przyjęta interpretacja
    # Pythona — nie coś, co użytkownik faktycznie powiedział. Gdy
    # wpisany tekst brzmi jak zgłoszenie problemu, note WPROST mówi
    # zespołowi, żeby NIE zakładał sukcesu; w pozostałych przypadkach
    # note jest neutralna (nie przesądza ani sukcesu, ani porażki) i
    # każe zespołowi samodzielnie ocenić treść.
    looks_like_failure = bool(user_typed) and _looks_like_failure_report(user_typed)

    log(
        "MAIN",
        "NEED_USER_LOGIN: użytkownik odpowiedział na prośbę o "
        "czynność ręczną na stronie " + (login_url or "(brak URL)") + "."
        + (
            " Wkleił tekst (" + str(len(user_typed)) + " znaków)."
            if user_typed else " Nacisnął Enter bez tekstu."
        )
        + (
            " UWAGA: treść wygląda na zgłoszenie problemu, nie "
            "potwierdzenie sukcesu."
            if looks_like_failure else ""
        )
    )

    if not user_typed:
        note = (
            "Użytkownik nacisnął Enter bez wpisywania tekstu — to "
            "literalny sygnał 'zrobione, kontynuuj' (dokładnie o to "
            "proszono w pytaniu). Sprawdź aktualny stan Chrome i "
            "kontynuuj, ale nadal zweryfikuj po stanie Chrome, czy "
            "czynność faktycznie się udała, zamiast ślepo ufać."
        )
    elif looks_like_failure:
        note = (
            "UWAGA: to NIE jest potwierdzenie sukcesu. Odpowiedź "
            "użytkownika (patrz \"user_provided_value\") brzmi jak "
            "ZGŁOSZENIE PROBLEMU/BŁĘDU (np. strona się nie wczytała, "
            "coś nie zadziałało) — użytkownik wprost mówi, że "
            "czynność NIE została wykonana. NIE zakładaj, że "
            "logowanie/czynność zostały ukończone. Sprawdź aktualny "
            "stan Chrome i zaplanuj kolejny krok uwzględniający ten "
            "problem (np. spróbuj otworzyć stronę jeszcze raz, "
            "zaproponuj inny adres, albo zapytaj użytkownika o więcej "
            "szczegółów) — NIE kontynuuj tak, jakby użytkownik był "
            "już zalogowany."
        )
    else:
        note = (
            "Użytkownik odpowiedział, wklejając poniższy tekst "
            "(patrz \"user_provided_value\"). NIE zakładaj "
            "automatycznie, że to potwierdzenie sukcesu — to może "
            "być: (a) faktyczna wartość do wykorzystania (klucz "
            "API/kod/numer), (b) potwierdzenie że czynność jest "
            "zrobiona, albo (c) coś innego. Przeczytaj treść i sam "
            "oceń, co faktycznie oznacza, zanim uznasz czynność za "
            "zakończoną. Jeśli to wartość — użyj jej BEZPOŚREDNIO w "
            "następnym kroku, np. każąc Gemini zapisać ją do "
            "właściwego pliku, zamiast zakładać że użytkownik już to "
            "gdzieś zapisał sam."
        )

        # Zaobserwowany realny bug (log 2026-08-27, cel: Auth Token
        # Twilio): użytkownik wkleił CAŁY fragment strony ("Live
        # credentials\nAccount SID...\nAuth token\n...\n93a29e032a...")
        # — prawdziwy token BYŁ w tym tekście, na ostatniej linii. Ale
        # PLANNER/ENGINEER spojrzeli tylko na PIERWSZĄ linię ("Live
        # credentials"), uznali to za samą etykietę interfejsu bez
        # wartości, i odrzucili całość, proszą użytkownika ponownie —
        # dokładnie ten sam błąd powtórzył się chwilę później z
        # napisem "Auth token". Dodajemy jawne ostrzeżenie, gdy
        # wklejony tekst wygląda na WIELOLINIOWY fragment strony
        # (a nie pojedynczą, czystą wartość) — żeby zespół PRZESZUKAŁ
        # całość zamiast oceniać po pierwszej linii.
        if "\n" in user_typed:
            note += (
                " UWAGA: wklejony tekst ma WIELE LINII — to wygląda "
                "na skopiowany fragment całej strony, nie samą "
                "wartość. Może zawierać etykiety interfejsu (np. "
                "\"Auth token\", \"Live credentials\", nazwy sekcji) "
                "WYMIESZANE z faktyczną wartością w INNEJ linii. NIE "
                "oceniaj po samej pierwszej linii i nie odrzucaj "
                "całości jako 'to tylko opis' — PRZEJRZYJ WSZYSTKIE "
                "linie i znajdź tę, która faktycznie wygląda jak "
                "oczekiwana wartość (długi ciąg losowych znaków "
                "alfanumerycznych, bez spacji), zanim uznasz że "
                "użytkownik nie podał właściwej wartości."
            )

    # Zapis wklejonej wartości pod STAŁĄ ścieżkę — jedyny kanał,
    # którym GEMINI (wykonawca piszący i uruchamiający skrypt) może ją
    # w ogóle dostać. Patrz komentarz przy USER_PROVIDED_VALUE_FILE:
    # bez tego Gemini pisało skrypty bez klucza i dostawało 401.
    value_file_note = ""

    if user_typed:
        try:
            USER_PROVIDED_VALUE_FILE.parent.mkdir(
                parents=True,
                exist_ok=True
            )
            USER_PROVIDED_VALUE_FILE.write_text(
                user_typed,
                encoding="utf-8"
            )

            # Sekret — czytelny wyłącznie dla właściciela.
            try:
                os.chmod(USER_PROVIDED_VALUE_FILE, 0o600)
            except Exception:
                pass

            value_file_note = (
                "\n\nWARTOŚĆ JEST ZAPISANA W PLIKU: "
                + str(USER_PROVIDED_VALUE_FILE)
                + "\nGemini NIE widzi treści tej rozmowy — jeżeli "
                "kolejny krok ma jej użyć (np. w nagłówku "
                "Authorization), TASK ma kazać ODCZYTAĆ ją z TEGO "
                "pliku (np. `KEY=$(cat "
                + str(USER_PROVIDED_VALUE_FILE)
                + ")`), zamiast wklejać samą wartość w treść "
                "zadania. Zaobserwowany realny przypadek: skrypt "
                "napisany bez dostępu do wartości dostał 401 "
                "AUTH_FAILURE, po czym szukał klucza po starych "
                "plikach i trafił na atrapę z poprzedniej sesji."
            )

            log(
                "MAIN",
                "Wklejona wartość zapisana do "
                + str(USER_PROVIDED_VALUE_FILE)
                + " (prawa 0600) — Gemini może ją odczytać z pliku."
            )

        except Exception as e:
            log(
                "MAIN",
                "Nie udało się zapisać wklejonej wartości do pliku: "
                + str(e)
            )

    return {
        "status": "USER_RESPONDED_TO_LOGIN_PROMPT",
        "url": login_url,
        "user_provided_value": user_typed or None,
        "user_provided_value_file": (
            str(USER_PROVIDED_VALUE_FILE) if value_file_note else None
        ),
        "note": note + value_file_note
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

    # Licznik "miękkich" przekierowań MAIN-a z powrotem do zespołu,
    # gdy prosi o dane kontaktowe (numer telefonu) BEZ wcześniejszej
    # próby sprawdzenia kontaktów na telefonie — patrz
    # _decision_asks_for_contact_info() przy _handle_need_user_login().
    contact_gate_redirects = 0

    # Zaobserwowany realny bug (log 2026-08-27, sesja puszczona na
    # noc bez nadzoru): to BYŁ `for step in range(1, MAX_STEPS + 1)`
    # — każda iteracja, WŁĄCZNIE z tymi, które tylko czekały na reset
    # limitu Gemini (blok "WYKONAWCA ZABLOKOWANY" niżej, sleep(30) +
    # continue), zużywała JEDEN z ograniczonej puli MAX_STEPS (domyślnie
    # 40) kroków. Log pokazał dokładnie to: po wyczerpaniu limitu API
    # (KROK 2) kolejne 38 kroków (KROK 3-40) to WYŁĄCZNIE czekanie co
    # 30s — realnie ok. 19 minut — po czym padło "OSIĄGNIĘTO LIMIT
    # KROKÓW" i CAŁY PROGRAM SIĘ ZAKOŃCZYŁ, mimo że własny komunikat
    # w kodzie mówi, że reset limitu Gemini trwa "zwykle 24h". Program
    # zostawiony na całą noc bez nadzoru realnie popracował kilka minut,
    # a resztę czasu leżał już zamknięty, czekając na nikogo. Naprawiono:
    # `step` (i limit MAX_STEPS) liczy TYLKO iteracje, w których agent
    # faktycznie coś zrobił — czekanie na reset limitu API kręci pętlę
    # w kółko (`continue`) BEZ zużywania budżetu kroków, więc program
    # faktycznie doczeka realnego resetu zamiast zamykać się na próżno.
    step = 0

    while step < MAX_STEPS:

        # ------------------------------------------------------
        # Jeśli Gemini quota jest wyczerpana, NIE twórz kolejnych
        # tasków i nie konsultuj teamu — to strata kredytów DeepSeek,
        # bo i tak nie możemy nic wykonać. Daj MAIN znać i poczekaj
        # na reset limitu. UMYŚLNIE PRZED zwiększeniem `step`/printem
        # "--- KROK ---" — patrz komentarz wyżej, to czekanie nie
        # jest krokiem agenta i nie ma zużywać budżetu MAX_STEPS.
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

        step += 1

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
        # Stan
        # ------------------------------------------------------

        # Pobrane RAZ na krok i przekazywane dalej do
        # estimate_progress()/consult_team()/main_decide() — patrz
        # komentarz w consult_team() o chrome_text/android_text.
        # Wcześniej każda z tych 3 funkcji pytała urządzenie o
        # DOKŁADNIE ten sam stan osobno (do 4 żywych zapytań ADB/CDP
        # na jeden krok agenta).
        step_chrome_text = chrome_summary()
        step_android_text = android_summary()

        log(
            "STATE",
            "Chrome: "
            + short(
                step_chrome_text,
                900
            )
        )

        log(
            "STATE",
            "Android: "
            + short(
                step_android_text,
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
                progress = estimate_progress(
                    goal, step_chrome_text, step_android_text
                )

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
            step,
            step_chrome_text,
            step_android_text
        )

        # ------------------------------------------------------
        # MAIN
        # ------------------------------------------------------

        raw = main_decide(
            goal,
            step,
            team,
            last_result,
            step_chrome_text,
            step_android_text
        )

        decision = parse_json(raw)

        if isinstance(decision, dict) and decision.get("type") == "ASK":

            decision = _handle_main_ask(
                decision,
                goal,
                step,
                team,
                last_result,
                step_chrome_text,
                step_android_text
            )

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
            # ZAPIS KODU ENGINEER BEZ UDZIAŁU GEMINI
            #
            # Jeżeli MAIN zdecydował, że gotowy blok kodu z
            # bieżącej odpowiedzi ENGINEER ma trafić
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
                            "ENGINEER do "
                            + write_target
                            + ", ale w jego ostatniej odpowiedzi "
                            "nie znaleziono bloku kodu (```...```). "
                            "Zapytaj ENGINEER "
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
                            "blok kodu od ENGINEER "
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
                            "poproś ENGINEER o czysty "
                            "kod pliku, bez komend powłoki wokół "
                            "niego."
                        )
                    }

                    continue

                # --------------------------------------------------
                # BEZPIECZEŃSTWO: lustrzane odbicie powyższego —
                # blok kodu wygląda jak PYTHON, a cel to .sh, więc
                # zostanie uruchomiony przez `bash` i posypie się
                # samymi błędami składni ("import: command not
                # found" itp.). Zaobserwowane naprawdę.
                # --------------------------------------------------

                if _looks_like_python_script(
                    engineer_code,
                    target_path
                ):

                    last_result = {
                        "status":
                            "ENGINEER_CODE_LOOKS_LIKE_PYTHON_SCRIPT",
                        "message": (
                            "write_engineer_code_to ODRZUCONE: "
                            "blok kodu od ENGINEER "
                            "wygląda jak PYTHON (zawiera "
                            "'import ...'/'from ... import'/"
                            "'def ...(' itp.), a docelowa ścieżka "
                            "to " + str(target_path) + " (.sh). "
                            "Uruchomienie tego przez `bash` zwróci "
                            "wyłącznie błędy składni ('import: "
                            "command not found' i podobne), nie "
                            "prawdziwy wynik. Jeżeli kod ma być "
                            "Pythonem — zmień docelową ścieżkę na "
                            ".py i każ Gemini uruchomić go przez "
                            "`python3 plik.py`, nie `bash plik.py`. "
                            "Jeżeli to miał być czysty Bash — "
                            "poproś ENGINEER o kod bez składni "
                            "Pythona."
                        )
                    }

                    continue

                # --------------------------------------------------
                # BEZPIECZEŃSTWO: gdy cel to .py, sprawdź NAPRAWDĘ,
                # czy to poprawny Python — zamiast dalej zgadywać
                # kolejne wzorce tekstowe (patrz docstring
                # _python_syntax_error). To łapie DOKŁADNIE ten
                # przypadek, którego _looks_like_shell_script() nie
                # złapał: "polecenie do uruchomienia" zapisane jako
                # "treść pliku" .py.
                # --------------------------------------------------

                if target_path.suffix.lower() == ".py":

                    syntax_error = _python_syntax_error(
                        engineer_code
                    )

                    if syntax_error:

                        last_result = {
                            "status":
                                "ENGINEER_CODE_INVALID_PYTHON_SYNTAX",
                            "message": (
                                "write_engineer_code_to ODRZUCONE: "
                                "docelowa ścieżka to " + str(target_path)
                                + " (.py), ale blok kodu od ENGINEER "
                                "NIE JEST poprawnym Pythonem — próba "
                                "kompilacji zwróciła: " + syntax_error
                                + ". To zwykle oznacza, że ENGINEER "
                                "podał POLECENIE URUCHOMIENIA (np. "
                                "\"cd ... && python plik.py\") zamiast "
                                "TREŚCI samego pliku. Poproś ENGINEER "
                                "o czystą treść pliku .py, bez "
                                "poleceń powłoki wokół niej — samo "
                                "uruchomienie (jeśli potrzebne) należy "
                                "do zwykłego TASKu, nie do tego bloku "
                                "kodu."
                            )
                        }

                        continue

                # --------------------------------------------------
                # BEZPIECZEŃSTWO: write_engineer_code_to NADPISUJE
                # cały plik. Jeżeli plik już istnieje i jest sporo
                # większy niż nowy blok kodu, to prawie na pewno
                # oznacza, że ENGINEER podał tylko
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
                                "ENGINEER ma tylko "
                                + str(new_size) + "B (mniej niż "
                                "40% obecnego rozmiaru). To wygląda "
                                "na FRAGMENT/poprawkę, nie cały "
                                "plik — nadpisanie zniszczyłoby "
                                "resztę. Jeżeli to naprawdę cała "
                                "nowa zawartość pliku, zmień "
                                "podejście (np. poproś "
                                "ENGINEER o "
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
                        "Zapisano kod ENGINEER "
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

                    # Zaobserwowany realny problem (2026-08-28, log
                    # "finalize.sh"): mimo że Python WŁAŚNIE zapisał
                    # gotowy kod ENGINEER do target_path (bez udziału
                    # Gemini), pole "task" napisane przez MAIN i tak
                    # kazało Gemini napisać ten sam plik jeszcze raz
                    # (termux_write_file) — Gemini nadpisał świeżo
                    # zapisany plik (391 B) krótszą, własną wersją
                    # (131 B), złapane dopiero przez ogólny,
                    # niespecyficzny mechanizm ostrzegania o nadpisaniu
                    # pliku (czysty przypadek, że oba warianty akurat
                    # zadziałały). MAIN_PROMPT od dawna mówi wprost, że
                    # "task" ma dotyczyć WYŁĄCZNIE uruchomienia/
                    # testowania — ale to tylko sugestia dla modelu,
                    # nie gwarancja, więc dopisujemy JEDNOZNACZNĄ,
                    # deterministyczną notatkę do treści zadania,
                    # niezależnie od tego, co MAIN faktycznie napisał.
                    task_text += (
                        "\n\n[AUTOMATYCZNA NOTATKA — PRZECZYTAJ]: "
                        "plik " + str(target_path) + " ZOSTAŁ JUŻ "
                        "ZAPISANY (gotowy kod ENGINEER, "
                        + str(len(engineer_code)) + " znaków) PRZED "
                        "tym zadaniem, bez Twojego udziału. NIE twórz "
                        "go ponownie i nie nadpisuj (termux_write_file"
                        "/cat/echo > itp.) — to zadanie dotyczy "
                        "WYŁĄCZNIE uruchomienia i przetestowania "
                        "pliku, który już tam jest."
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

            duplicate_msg = _checklist_duplicate_message(task_text)

            if duplicate_msg:

                last_result = {
                    "status": "TASK_DUPLICATE_OF_VERIFIED_POINT",
                    "message": duplicate_msg
                }

                continue

            already_satisfied_msg = (
                _success_condition_already_satisfied_message(
                    task_text, success_condition
                )
            )

            if already_satisfied_msg:

                last_result = {
                    "status": "TASK_ALREADY_SATISFIED_ON_DISK",
                    "message": already_satisfied_msg
                }

                continue

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
        # NEED_USER_LOGIN
        # ------------------------------------------------------
        #
        # Zamiast FAILED, gdy JEDYNYM blokerem jest czynność, którą
        # fizycznie musi wykonać człowiek w przeglądarce (login,
        # kod SMS, CAPTCHA, zgoda na koncie usługi zewnętrznej) —
        # Gemini nie potrafi tego wpisać ani ominąć. Patrz
        # _handle_need_user_login() — ta sama obsługa jest używana
        # też przy alternatywie po FAILED niżej.

        if dtype == "NEED_USER_LOGIN":

            last_result, contact_gate_redirects = (
                _need_user_login_with_contact_gate(
                    decision, contact_gate_redirects
                )
            )

            continue

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

                # Brak wykonawcy Gemini (np. wygasła/wyczerpana
                # quota) to problem ZEWNĘTRZNY wobec samego celu —
                # naprawia się go np. rotacją klucza, nie zmianą
                # celu. Zaobserwowany realny problem: usuwanie
                # GOAL_FILE tutaj zmuszało użytkownika do wklejania
                # CAŁEGO wieloliniowego tekstu celu od nowa po
                # KAŻDEJ takiej przerwie, mimo że chodziło o
                # dokładnie ten sam test — a ponowne wklejenie tego
                # samego tekstu jest odczytywane jako "NOWY cel",
                # czyli dodatkowo resetuje 9 sesji DeepSeek (v46) bez
                # potrzeby. Zostawiamy GOAL_FILE, żeby po naprawieniu
                # klucza dało się po prostu wcisnąć Enter.
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

                    duplicate_msg = _checklist_duplicate_message(
                        task_text
                    )

                    if duplicate_msg:

                        last_result = {
                            "status":
                                "TASK_DUPLICATE_OF_VERIFIED_POINT",
                            "message": duplicate_msg
                        }

                        continue

                    already_satisfied_msg = (
                        _success_condition_already_satisfied_message(
                            task_text, condition
                        )
                    )

                    if already_satisfied_msg:

                        last_result = {
                            "status":
                                "TASK_ALREADY_SATISFIED_ON_DISK",
                            "message": already_satisfied_msg
                        }

                        continue

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

            # Zaobserwowany realny bug (log 2026-08-26): ta ścieżka
            # sprawdzała WYŁĄCZNIE "type"=="TASK" — gdy MAIN, poproszony
            # "sprawdź jeszcze raz, czy istnieje alternatywa", sam
            # zwrócił NEED_USER_LOGIN (np. "wygeneruj klucz API
            # ręcznie"), było to po cichu ignorowane i sesja kończyła
            # się zwykłym FAILED, mimo że MAIN dosłownie właśnie podał
            # wykonalną drogę do przodu. Patrz _handle_need_user_login().
            if (
                alt
                and str(alt.get("type", "")).upper()
                == "NEED_USER_LOGIN"
            ):

                last_result, contact_gate_redirects = (
                    _need_user_login_with_contact_gate(
                        alt, contact_gate_redirects
                    )
                )

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

            # Zaobserwowany realny problem: FAILED tutaj bardzo
            # często wynika z prawdziwego BUGA w agent.py (dokładnie
            # jak w tej sesji naprawczej — chrome_open, kontrakt
            # custom_tools, itd.), nie z tego, że CEL jest
            # niewykonalny. Usuwanie GOAL_FILE zmuszało użytkownika
            # do ręcznego wklejania całego, wieloliniowego tekstu
            # celu od nowa po KAŻDEJ takiej porażce, żeby ponownie
            # przetestować DOKŁADNIE to samo — a ponowne wklejenie
            # tego samego tekstu jest odczytywane jako NOWY cel,
            # więc dodatkowo resetowało 9 sesji DeepSeek (v46) bez
            # potrzeby. Zostawiamy GOAL_FILE: po naprawieniu kodu
            # wystarczy Enter, żeby wznowić DOKŁADNIE ten sam cel
            # (i te same sesje zespołu).
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

def maybe_restart_team_sessions_for_new_goal():
    """
    Pyta, czy wyczyścić trwały stan 9 sesji DeepSeek zespołu PRZED
    ich załadowaniem (init_team()) — samo czyszczenie plików stanu,
    BEZ wołania init_team() (to robi main(), RAZ, zaraz po tej
    funkcji, niezależnie od odpowiedzi — dzięki temu sesje są
    ładowane dokładnie raz, nigdy dwa razy pod rząd: wznów, a potem
    ewentualnie wyczyść-i-wznów-ponownie).

    Od v113 pytana JAWNIE, na samym starcie programu, PRZED
    init_deepseek()/init_team() — a nie automatycznie tylko wtedy,
    gdy typed cel różni się od zapisanego. Wcześniej (v31-v112):
    init_team() ładowało 9 sesji ZE STARĄ historią bez pytania, a
    dopiero PO wpisaniu nowego, innego celu ta funkcja kasowała je
    i ładowała drugi raz — dwa pełne rundy inicjalizacji DeepSeek za
    każdym razem, gdy cel się zmieniał. Teraz to jedno, jawne
    pytanie na starcie, niezależne od tego, jaki cel użytkownik
    poda później.
    """

    if not _confirm_destructive_action(
        "RESET 9 SESJI DEEPSEEK (MAIN, PLANNER, RESEARCHER, "
        "CRITIC, BROWSER, CODE_REVIEWER, CODE_FIXER, "
        "ENGINEER, PROGRESS_ESTIMATOR) — zespół zacznie od zera, "
        "bez pamięci poprzedniej rozmowy (świeży system_prompt dla "
        "każdej roli)"
    ):
        log(
            "DEEPSEEK",
            "Zachowano stare sesje zespołu (użytkownik odmówił "
            "resetu)."
        )
        return

    for name in _ROLE_ACCOUNT:
        _clear_session_state(name)

    # Nowy, niepowiązany cel nie powinien dziedziczyć "już otwarte w
    # tym celu" ostrzeżeń o aplikacjach z POPRZEDNIEGO celu — inaczej
    # android_launch_app fałszywie ostrzegałby o powtórce, mimo że to
    # w istocie pierwsze uruchomienie w kontekście nowego celu.
    _confirmed_app_launches.clear()

    log(
        "DEEPSEEK",
        "Wyczyszczono stan 9 sesji zespołu — załadują się od zera "
        "(bez kontekstu poprzedniej rozmowy)."
    )


def maybe_clear_previous_session_data():
    """
    Wołane na SAMYM STARCIE programu (v113), przed init_team(),
    niezależnie od tego, jaki cel użytkownik później poda — pyta
    czy usunąć dane poprzedniej sesji: kolejkę zadań, zapisane
    wyniki, licznik powtarzających się porażek narzędzi, ostatni
    wynik. Wcześniej (v31-v112) pytane dopiero PO wpisaniu celu, i
    tylko gdy różnił się od zapisanego.

    Bez tego: jeśli poprzednia sesja padła z zadaniem w stanie
    PENDING w kolejce, run_agent() na pierwszym kroku podjąłby i
    wykonał to stare zadanie — dotyczące poprzedniego, niepowiązanego
    celu — zanim w ogóle skonsultowałby nowy.

    Nie dotyka custom_tools/ (to trwałe, celowo dodane narzędzia,
    nie dane sesji) ani samego GOAL_FILE (to obsługiwane osobno,
    tam gdzie ta funkcja jest wołana). Zrzuty ekranu NIE są już tu
    obsługiwane — mają teraz jawną, trwałą zgodę użytkownika na
    automatyczne kasowanie bez pytania, patrz
    _cleanup_screenshots_silently() (atexit + start programu).
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
        GEMINI_STATE_FILE,
        PROGRESS_CHECKLIST_FILE,
        APPROACHES_FILE,
        CONTACTS_LOOKUP_ATTEMPTED_FILE,
        USER_PROVIDED_VALUE_FILE
    ):
        try:
            extra.unlink(missing_ok=True)
        except Exception:
            pass

    log(
        "MAIN",
        "Usunięto dane poprzedniej sesji (kolejka, wyniki, "
        "licznik prób narzędzi, ostatni wynik, checklist punktów)."
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


def maybe_clear_custom_tools():
    """
    OSOBNE, opt-in pytanie od maybe_clear_generated_project_files().

    Zaobserwowany realny problem (log 2026-08-24, test "uniwersalny"):
    narzędzia w custom_tools/ NIGDY nie pojawiają się na liście do
    skasowania przy 'wyczysc'/nowym celu — cały ~/agent/ (gdzie
    fizycznie leży custom_tools/) jest wykluczony ze śledzenia
    _track_project_path(), bo w tym samym katalogu leżą agent.py i
    klucze API, których TA lista nigdy nie może dotknąć. Efekt
    uboczny: użytkownik, widząc ogólne pytanie o "wygenerowane pliki
    projektu", rozsądnie zakładał, że obejmuje to WSZYSTKO — a
    narzędzie z zupełnie innego, dawnego celu (np. mnoznik.py) wciąż
    tam było i kolidowało z nowym celem każącym "stwórz NOWE
    narzędzie" o tej samej nazwie (patrz _goal_progress_snapshot —
    v96 dodał tam oznaczenie "z tego celu"/"sprzed tego celu"
    właśnie z tego powodu).

    Custom_tools są jednak CELOWO pomyślane jako trwały, wielokrotnego
    użytku zestaw (patrz MAIN_PROMPT — mają być wywoływane WIELOKROTNIE
    z różnymi argumentami w przyszłych celach), więc nie czyścimy ich
    automatycznie razem z resztą — pytamy o to OSOBNO, jawnie, z pełną
    listą, żeby to była świadoma decyzja, nie przypadkowa strata
    przydatnego narzędzia przy zwykłym sprzątaniu.
    """

    try:
        tool_files = sorted(
            p for p in CUSTOM_TOOLS_DIR.glob("*.py")
            if p.name != "__init__.py"
        )
    except Exception:
        tool_files = []

    if not tool_files:
        return

    print()
    print(
        "Zarejestrowane narzędzia niestandardowe (custom_tools/ — "
        "TRWAŁE, NIE objęte zwykłym sprzątaniem powyżej):"
    )

    for p in tool_files:
        try:
            size = p.stat().st_size
        except Exception:
            size = "?"
        print("  - " + str(p) + " (" + str(size) + " B)")

    confirm_message = (
        "USUNIĘCIE NARZĘDZI NIESTANDARDOWYCH powyżej (custom_tools/) "
        "— to OSOBNA decyzja od zwykłego sprzątania: te narzędzia są "
        "pomyślane jako trwałe, do wielokrotnego użytku w przyszłych "
        "celach, nie jednorazowe dane projektu"
    )

    if not _confirm_destructive_action(confirm_message):
        log(
            "MAIN",
            "Zachowano narzędzia niestandardowe (custom_tools/)."
        )
        return

    for p in tool_files:

        try:
            p.unlink()
            log(
                "MAIN",
                "Usunięto narzędzie niestandardowe: " + str(p)
            )
        except Exception as e:
            log(
                "MAIN",
                "Nie udało się usunąć " + str(p) + ": " + str(e)
            )

    load_custom_tools()


def maybe_clear_stale_final_ok_evidence():
    """
    OSOBNE, opt-in pytanie o pliki-DOWODY FINAL_OK.txt.

    Zaobserwowany realny problem (log 2026-08-26, cel "zadzwoń do
    Beaty"): ~/FINAL_OK.txt (treść "TEST_V73_ZAKONCZONY", sprzed
    wielu dni) i ~/agent/apk_output/FINAL_OK.txt (treść z zupełnie
    innego, wcześniejszego zadania "ZADZWONIONO DO BEATY...")
    leżały na dysku od dawna. Żaden z nich nie mógł trafić do
    zwykłego sprzątania wygenerowanych plików: ~/FINAL_OK.txt
    powstał najpewniej przez surową komendę powłoki (np.
    `echo ... > FINAL_OK.txt` przez termux_run) — to całkowicie
    omija _track_project_path() — a apk_output/ jest CELOWO
    wykluczony ze śledzenia (patrz
    _AGENT_DIR_PROTECTED_SECOND_LEVEL_NAMES), bo w tym katalogu mają
    też prawo trwale leżeć prawdziwe zbudowane APK-i. Efekt: te 2
    pliki zaśmiecały KAŻDY kolejny, niepowiązany cel — _goal_
    progress_snapshot() naprawiono osobno, żeby ich TREŚCI nie
    pokazywać zespołowi jako aktualnej (patrz świeżość vs
    GOAL_FILE), ale same pliki nadal fizycznie zaśmiecały dysk.

    CELOWO tylko te 3 znane, sztywne ścieżki (te same, które
    sprawdza verify_final()/_goal_progress_snapshot()) — NIGDY
    żaden skan katalogu domowego (patrz ostrzeżenie w
    maybe_clear_generated_project_files() o ~/api_token.txt i
    ~/test/pow_helper.js skasowanych przez taki skan w przeszłości).
    """

    candidates = [
        HOME / "FINAL_OK.txt",
        AGENT_DIR / "FINAL_OK.txt",
        APK_OUTPUT_DIR / "FINAL_OK.txt",
    ]

    found = []

    for p in candidates:

        content = read_text(p).strip()

        if content:
            found.append((p, content))

    if not found:
        return

    print()
    print(
        "Pliki-DOWODY FINAL_OK.txt znalezione na dysku (mogą być "
        "pozostałością po wcześniejszych, niepowiązanych zadaniach "
        "— sprawdź treść/datę poniżej):"
    )

    for p, content in found:

        try:
            mtime = datetime.fromtimestamp(
                p.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = "?"

        print(
            "  - " + str(p) + " (" + mtime + ") — \""
            + content[:60] + "\""
        )

    confirm_message = (
        "USUNIĘCIE PLIKÓW-DOWODÓW FINAL_OK.txt powyżej — to osobna "
        "decyzja od zwykłego sprzątania; mogą pochodzić z zupełnie "
        "innych, wcześniejszych zadań i nie są już potrzebne"
    )

    if not _confirm_destructive_action(confirm_message):
        log(
            "MAIN",
            "Zachowano pliki-dowody FINAL_OK.txt."
        )
        return

    for p, _content in found:

        try:
            p.unlink()
            log(
                "MAIN",
                "Usunięto plik-dowód: " + str(p)
            )
        except Exception as e:
            log(
                "MAIN",
                "Nie udało się usunąć " + str(p) + ": " + str(e)
            )


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


def _read_full_input(prompt):
    """
    input() zwraca tylko PIERWSZĄ linię ze stdin. Jeżeli użytkownik
    WKLEJA wieloliniowy tekst do terminala Termuksa (a nie wpisuje
    go znak po znaku), cała wklejka trafia do bufora stdin naraz —
    input() odda tylko pierwszą linię, a REST zostanie w buforze i
    zostanie PO CICHU skonsumowany przez KOLEJNE wywołanie input(),
    bez żadnej realnej interakcji użytkownika.

    Realny, zaobserwowany przypadek: użytkownik wkleił 8-liniowy
    cel. Zamiast całego tekstu, agent zapisał jako CEL tylko
    pierwszą linię — a dwa NASTĘPNE pytania potwierdzające
    usunięcie plików ("Zezwolić? [t/N]") zostały automatycznie
    "odpowiedziane" resztkami tej samej wklejki, zanim użytkownik
    zdążył cokolwiek nacisnąć. To nie tylko ucina cel — to realne
    ryzyko bezpieczeństwa: gdyby któraś z pozostałych linii wklejki
    zaczynała się od "tak"/"t", operacja usuwania zostałaby
    zaakceptowana bez żadnej świadomej zgody użytkownika, dokładnie
    tego typu incydent, po którym dodaliśmy te pytania.

    Fix: po pierwszej linii z input() dociągamy WSZYSTKIE dodatkowe
    linie, które są JUŻ dostępne w buforze stdin w tej chwili (bez
    czekania na nowe dane od użytkownika) — odtwarzając całą
    wklejkę jako jeden tekst, zamiast zostawiać resztę do
    przypadkowej konsumpcji przez późniejsze input().
    """

    first_line = input(prompt)
    lines = [first_line]

    try:
        while select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
    except Exception:
        pass

    return "\n".join(lines)


_PERMISSION_TEST_COMMAND_WORDS = {
    "uprawnienia", "uprawnienie", "permissions"
}


def _run_permission_bootstrap():
    """
    Na żądanie użytkownika (komenda 'uprawnienia' na prompcie CELU)
    odpala po kolei serię PRAWDZIWYCH komend termux-api, żeby
    wywołać naturalne okienka Androida "Zezwól/Odmów" dla
    WSZYSTKICH uprawnień, których agent może potrzebować w trakcie
    działania — zamiast trafiać na nie pojedynczo, w środku
    autonomicznej sesji, kiedy nikt nie stoi przy telefonie, żeby
    kliknąć.

    Zaobserwowany realny przypadek: użytkownik ręcznie odkrywał to
    krok po kroku w rozmowie z asystentem (kontakty, potem osobno
    bateria/DuraSpeed/Autostart w Ustawieniach systemu, potem
    lokalizacja/aparat/mikrofon/powiadomienia) — ta funkcja robi
    komplet jedną komendą.

    CELOWO NIE testuje termux-telephony-call — to naprawdę
    zadzwoniłoby do kogoś. To uprawnienie (CALL_PHONE) i tak
    zapyta samo przy pierwszej prawdziwej próbie połączenia
    zrobionej przez agenta w trakcie realizacji celu.
    """

    print()
    print(
        "Uruchamiam po kolei prawdziwe komendy termux-api, żeby "
        "wywołać okienka Androida z prośbą o zgodę — dla KAŻDEGO "
        "pojawiającego się okienka wybierz 'Zezwól'/'Allow' na "
        "telefonie. NIE testuję termux-telephony-call (to naprawdę "
        "dzwoni) — to uprawnienie zapyta samo przy pierwszej "
        "prawdziwej próbie połączenia w trakcie celu."
    )
    print()

    photo_path = str(HOME / ".agent_perm_test_photo.jpg")
    audio_path = str(HOME / ".agent_perm_test_audio.wav")

    checks = (
        ("termux-battery-status", "bateria (kontrolne — nie wymaga zgody)"),
        ("termux-location -p network -r once", "lokalizacja"),
        ("termux-camera-photo -c 0 " + photo_path, "aparat"),
        (
            "termux-microphone-record -f " + audio_path + " -l 2",
            "mikrofon"
        ),
        ("termux-call-log -l 1", "historia połączeń"),
        ("termux-contact-list", "kontakty"),
        (
            "termux-telephony-deviceinfo",
            "telefonia/sieć (BEZ dzwonienia)"
        ),
        (
            "termux-notification -t 'Test agenta' "
            "-c 'To tylko test uprawnien.'",
            "powiadomienia"
        ),
    )

    for command, label in checks:

        print("-> " + label + " (" + command.split()[0] + ")")

        # UWAGA (zaobserwowany realny problem, 2026-08-26): 15s bywa
        # za krótkie na to, żeby użytkownik w ogóle ZDĄŻYŁ zauważyć
        # świeże okienko Androida "Zezwól/Odmów" i je kliknąć —
        # zwłaszcza zaraz po starcie programu, gdy telefon jeszcze
        # "dogania się" po uruchomieniu. Zbyt krótki timeout dawał
        # fałszywe "Błąd/timeout" nawet gdy użytkownik faktycznie
        # kliknąłby "Zezwól", gdyby miał więcej czasu.
        result = execute_shell(command, timeout=30)

        if result.get("ok"):
            print("   OK")
        else:
            print(
                "   Błąd/timeout — jeśli okienko o zgodę się "
                "pojawiło i kliknąłeś 'Zezwól', uruchom całą "
                "komendę 'uprawnienia' jeszcze raz."
            )

    for cleanup_path in (photo_path, audio_path):
        try:
            Path(cleanup_path).unlink()
        except Exception:
            pass

    print()
    print(
        "Test uprawnień zakończony. Jeśli któreś okienko się nie "
        "pojawiło mimo błędu — sprawdź ręcznie w Ustawieniach > "
        "Aplikacje > Termux:API > Uprawnienia (a baterię/Autostart/"
        "DuraSpeed w systemowych ustawieniach baterii)."
    )
    print()


def main():

    banner()

    # Sprząta ewentualne zrzuty ekranu z poprzedniego, twardo
    # zabitego uruchomienia (np. zamknięte przed atexit) — bez
    # pytania, patrz _cleanup_screenshots_silently().
    _cleanup_screenshots_silently()

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
    # Uprawnienia Termux:API — PRZED załadowaniem DeepSeek/Gemini,
    # żeby ewentualne braki (aparat, mikrofon, lokalizacja, kontakty,
    # powiadomienia) wyszły na jaw na samym starcie, a nie w środku
    # autonomicznej sesji. Opcjonalne — Enter/puste pomija.
    # ----------------------------------------------------------

    try:

        typed_perm_check = input(
            "Sprawdzić/aktywować uprawnienia Termux:API przed "
            "startem? [t/N] > "
        ).strip().lower()

    except (EOFError, KeyboardInterrupt):

        print()
        typed_perm_check = ""

    if typed_perm_check in ("t", "tak", "y", "yes"):
        _run_permission_bootstrap()

    # ----------------------------------------------------------
    # DeepSeek
    # ----------------------------------------------------------

    if not init_deepseek():
        sys.exit(1)

    # ----------------------------------------------------------
    # Sprzątanie/reset — PRZED załadowaniem sesji zespołu (v113),
    # zamiast (jak wcześniej, v31-v112) automatycznie tylko wtedy,
    # gdy wpisany cel różni się od zapisanego. Jedno jawne pytanie
    # na starcie, niezależnie od tego, jaki cel zapadnie później —
    # dzięki temu init_team() poniżej ładuje sesje dokładnie RAZ
    # (świeże, jeśli zresetowano stan powyżej, wznowione w
    # przeciwnym razie), zamiast wznawiać stare i ewentualnie zaraz
    # potem kasować je i wznawiać drugi raz.
    # ----------------------------------------------------------

    maybe_clear_previous_session_data()
    maybe_clear_generated_project_files()
    maybe_clear_stale_final_ok_evidence()
    maybe_clear_custom_tools()
    maybe_restart_team_sessions_for_new_goal()

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

    # Słowo-klucz "wyczysc" na tym prompcie odpala sprzątanie NA
    # ŻĄDANIE, niezależnie od tego, czy zaraz potem podasz nowy cel,
    # czy wznowisz stary — bez tego jedyne momenty sprzątania to
    # start NOWEGO celu (v46-v47) i zamknięcie programu (screenshoty,
    # v50). Po sprzątnięciu program wraca do tego samego pytania o
    # cel, nie kończy działania.
    _CLEAN_COMMAND_WORDS = {
        "wyczysc", "wyczyść", "czysc", "czyść", "clean"
    }

    def _run_on_demand_cleanup():

        before = (
            list(QUEUE_DIR.glob("*.json"))
            + list(RESULTS_DIR.glob("*.json"))
        )

        _cleanup_screenshots_silently()
        maybe_clear_previous_session_data()
        maybe_clear_generated_project_files()
        maybe_clear_stale_final_ok_evidence()
        maybe_clear_custom_tools()

        if not before:
            print(
                "Brak danych do posprzątania (kolejka/wyniki już "
                "puste) — zrzuty ekranu i tak sprzątnięte przy "
                "okazji."
            )

        print(
            "Sprzątanie zakończone. Zapisany CEL NIE został "
            "usunięty (to celowe — możesz go wznowić) — Enter "
            "wznowi go teraz, albo wpisz nowy cel."
        )
        print()

    try:

        while True:

            if saved_goal:

                print(
                    "Wykryto niedokończoną sesję:"
                )
                print(
                    "  " + short(saved_goal, 300)
                )
                print(
                    "Enter = wznów ten cel. Albo wpisz nowy cel, "
                    "żeby go zastąpić. Albo wpisz 'wyczysc', żeby "
                    "posprzątać wygenerowane pliki bez podawania "
                    "celu. Albo wpisz 'uprawnienia', żeby przetestować/"
                    "aktywować uprawnienia Termux:API (aparat, "
                    "mikrofon, lokalizacja, kontakty, powiadomienia)."
                )
                print()

                typed = _read_full_input(
                    "CEL AGENTA [Enter = wznów] > "
                ).strip()

                if typed.lower() in _CLEAN_COMMAND_WORDS:
                    _run_on_demand_cleanup()
                    continue

                if typed.lower() in _PERMISSION_TEST_COMMAND_WORDS:
                    _run_permission_bootstrap()
                    continue

                goal = typed if typed else saved_goal

            else:

                typed = _read_full_input(
                    "Podaj CEL AGENTA (albo 'wyczysc' żeby "
                    "posprzątać, albo 'uprawnienia' żeby przetestować "
                    "uprawnienia Termux:API) > "
                ).strip()

                if typed.lower() in _CLEAN_COMMAND_WORDS:
                    _run_on_demand_cleanup()
                    continue

                if typed.lower() in _PERMISSION_TEST_COMMAND_WORDS:
                    _run_permission_bootstrap()
                    continue

                goal = typed

            break

    except KeyboardInterrupt:

        print()
        return

    if not goal:

        print(
            "Brak celu."
        )

        return

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
