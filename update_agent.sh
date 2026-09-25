#!/data/data/com.termux/files/usr/bin/bash
# update_agent.sh — pobiera najnowszy agent.py z GitHuba i podmienia
# ~/agent/agent.py. Wszystko, co robi, zapisuje do ~/agent/update.log.
#
# Repozytorium trzyma w ukrytym katalogu ~/.ael-repo — agent nie sledzi
# katalogow z kropka, wiec sprzatanie na starcie nigdy go nie ruszy
# (stary ~/bot zniknal wlasnie przy sprzataniu).

REPO="https://github.com/mac41311-lgtm/bot.git"
BRANCH="claude/android-termux-agent-repair-18dvrp"
SRC="$HOME/.ael-repo"
DST="$HOME/agent"
LOG="$DST/update.log"

mkdir -p "$DST"
say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
fail() { say "BŁĄD: $*"; say "Szczegóły wyżej i w $LOG"; exit 1; }

say "=== aktualizacja agenta ==="

command -v git >/dev/null 2>&1 || { say "brak git — instaluję"; pkg install -y git >>"$LOG" 2>&1 || fail "nie udało się zainstalować git"; }

if [ -d "$SRC/.git" ]; then
    say "pobieram zmiany do $SRC"
    git -C "$SRC" fetch --depth 1 origin "$BRANCH" >>"$LOG" 2>&1 || fail "git fetch nie przeszedł (sieć?)"
    git -C "$SRC" reset --hard FETCH_HEAD >>"$LOG" 2>&1 || fail "git reset nie przeszedł"
else
    say "klonuję repozytorium do $SRC"
    rm -rf "$SRC"
    git clone --depth 1 -b "$BRANCH" "$REPO" "$SRC" >>"$LOG" 2>&1 || fail "git clone nie przeszedł (sieć?)"
fi

[ -f "$SRC/agent.py" ] || fail "w repozytorium nie ma agent.py"
python -m py_compile "$SRC/agent.py" >>"$LOG" 2>&1 || fail "nowy agent.py ma błąd składni — zostawiam stary"

KOPIA="$DST/_repo_sync_backup_$(date '+%Y%m%d_%H%M%S')"
mkdir -p "$KOPIA"
[ -f "$DST/agent.py" ] && cp "$DST/agent.py" "$KOPIA/" && say "kopia starego: $KOPIA/agent.py"

for f in agent.py jak_to_dziala.txt kto_co_dostaje.txt; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DST/$f"
done
chmod +x "$DST/agent.py"

# ten skrypt tez z repozytorium — nastepnym razem pojdzie juz nowy
[ -f "$SRC/update_agent.sh" ] && cp "$SRC/update_agent.sh" "$HOME/update_agent.sh" && chmod +x "$HOME/update_agent.sh"

# zostaw 5 ostatnich kopii
ls -1dt "$DST"/_repo_sync_backup_* 2>/dev/null | tail -n +6 | xargs -r rm -rf

say "gotowe: $(grep -m1 -o 'AEL-MINI AUTONOMOUS AGENT v[0-9]*' "$DST/agent.py") (commit $(git -C "$SRC" rev-parse --short HEAD))"
