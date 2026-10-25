#!/data/data/com.termux/files/usr/bin/bash
# Sprzatanie smieci po sesjach rywalizacji. Lista z logow (15-25.10) i ze
# skanu telefonu (skan.txt, 2026-10-25).
#
#   bash sprzatanie_po_sesji.sh            — tylko POKAZUJE, co by usunal
#   bash sprzatanie_po_sesji.sh usun       — usuwa smieci
#   bash sprzatanie_po_sesji.sh usun duze  — do tego pip-cache i stare venv-y
#                                            w Debianie (~7.6 GB, biblioteki
#                                            da sie zainstalowac od nowa)
#
# Rusza tylko rzeczy starsze niz PRZED (domyslnie: teraz). Przy dzialajacych
# druzynach podaj czas ich startu, np.:
#   PRZED="2026-10-25 07:40 UTC" bash sprzatanie_po_sesji.sh
# (czas z "UTC" — Debian liczy w UTC, Termux w czasie telefonu)
#
# Nigdy nie rusza: ~/agent, ~/MAIN_1, ~/MAIN_2, ~/RYWALIZACJA, ~/test
# (pow_helper.js dla DeepSeek), ~/.ael-repo, ~/storage, tokenow i kluczy,
# update_agent.sh, /opt/venv w Debianie, Android/.gradle/.cargo/vosk.

PRZED="${PRZED:-$(date -u '+%Y-%m-%d %H:%M UTC')}"
TRYB="${1:-pokaz}"
DUZE="${2:-}"
H="$HOME"
T="${PREFIX:-/data/data/com.termux/files/usr}/tmp"
echo "Rzeczy starsze niż: $PRZED   tryb: $TRYB $DUZE"

stare() { { [ -e "$1" ] || [ -L "$1" ]; } && [ -n "$(find "$1" -maxdepth 0 ! -newermt "$PRZED" 2>/dev/null)" ]; }
zrob() {
    if [ "$TRYB" = "usun" ]; then rm -rf -- "$1" && echo "usunięto  $1"
    else echo "do usunięcia  $1  ($(du -sh "$1" 2>/dev/null | cut -f1))"; fi
}
shopt -s nullglob

echo "== ~ (Termux): pliki drużyn spoza ich folderów"
for p in "$H"/agent_background_*.log "$H"/install_tf_debian.log "$H"/proot_install.log \
         "$H"/ubuntu_pip.log "$H"/ubuntu_torch.log "$H"/debian_torch*.log "$H"/distill_run.log \
         "$H"/rrp_err.txt "$H"/sprawdz_*.py "$H"/sprawdz_*_diag.log "$H"/sandbox_v2.py \
         "$H"/proposer_cv_v*.py "$H"/diag_proposer_*.py "$H"/baudrillard_predict.py \
         "$H"/fake_labeled.csv "$H"/src "$H"/runs "$H"/logs "$H"/onnx_test "$H"/tmp \
         "$H"/projekt "$H"/archiwum_rywalizacji "$H"/rsi.bak.faza11.* "$H"/FINAL_OK.txt \
         "$H"/__pycache__; do
    stare "$p" && zrob "$p"
done
# stare kopie zapasowe agent.py z aktualizacji: zostaja 2 najnowsze
for p in $(ls -1d "$H"/agent/_repo_sync_backup_* 2>/dev/null | sort | head -n -2); do
    stare "$p" && zrob "$p"
done

echo
echo "== tmp Termuksa ($T)"
for p in "$T"/tmp* "$T"/pip-* \
         "$T"/apt-dpkg-install-* "$T"/proot-*-* "$T"/main1_kwarantanna "$T"/eval_ep4.py \
         "$T"/e.txt "$T"/sogo_err.txt "$T"/adb.*.log; do
    stare "$p" && zrob "$p"
done

echo
echo "== Debian (od środka, przez proot-distro)"
if command -v proot-distro >/dev/null 2>&1; then
    proot-distro login debian -- env PRZED="$PRZED" TRYB="$TRYB" DUZE="$DUZE" bash -c '
        stare() { [ -e "$1" ] && [ -n "$(find "$1" -maxdepth 0 ! -newermt "$PRZED" 2>/dev/null)" ]; }
        zrob() { if [ "$TRYB" = "usun" ]; then rm -rf -- "$1" && echo "usunięto  (Debian) $1"
                 else echo "do usunięcia  (Debian) $1  ($(du -sh "$1" 2>/dev/null | cut -f1))"; fi; }
        shopt -s nullglob
        for p in /root/*; do
            [ -f "$p/pyvenv.cfg" ] && continue
            stare "$p" && zrob "$p"
        done
        for p in /tmp/perf-*.map /tmp/pip-* /tmp/tmp* /tmp/maml_sanity.json /tmp/stdlib_list.txt \
                 /tmp/torchinductor_root; do
            stare "$p" && zrob "$p"
        done
        echo "-- duże (biblioteki, da się zainstalować od nowa):"
        for p in /root/.cache /root/m2_venv /root/nli_venv; do
            [ -e "$p" ] || continue
            if [ "$DUZE" = "duze" ]; then stare "$p" && zrob "$p"
            else echo "zostaje bez \"duze\"  (Debian) $p  ($(du -sh "$p" 2>/dev/null | cut -f1))"; fi
        done
        echo "zostaje zawsze  (Debian) /opt/venv  ($(du -sh /opt/venv 2>/dev/null | cut -f1)) — torch, którego używają drużyny"
    ' 2>/dev/null || echo "(Debian się nie otworzył — ta część pominięta)"
fi

echo
echo "== Twoje starsze rzeczy (sprzed rywalizacji) — tylko pokazuję, usuń sam, jeśli niepotrzebne:"
for p in "$H"/Android "$H"/.gradle "$H"/android-jar "$H"/.cargo "$H"/vosk-model-small-pl-0.22 \
         "$H"/.npm "$H"/downloads "$H"/rsi "$H"/zalazek_AI; do
    [ -e "$p" ] && echo "  $p  ($(du -sh "$p" 2>/dev/null | cut -f1))"
done

[ "$TRYB" = "usun" ] || { echo; echo "Nic nie usunąłem. Żeby usunąć: bash $0 usun   (albo: usun duze)"; }
