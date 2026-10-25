#!/data/data/com.termux/files/usr/bin/bash
# Sprzatanie smieci po poprzedniej sesji rywalizacji (do 2026-10-25 08:40),
# przy dzialajacych druzynach. Lista nazw wzieta z logow sesji 15-25.10.
#
#   bash sprzatanie_po_sesji.sh        — tylko POKAZUJE, co by usunal
#   bash sprzatanie_po_sesji.sh usun   — usuwa
#
# Rusza tylko rzeczy starsze niz PRZED (domyslnie 2026-10-25 08:40), wiec
# dzisiejsza praca nowych druzyn zostaje. Nigdy nie rusza: ~/agent*,
# ~/MAIN_1, ~/MAIN_2, ~/RYWALIZACJA, ~/test (pow_helper.js dla DeepSeek),
# ~/bot, ~/storage, tokenow, update_agent.sh, plikow z kropka, bibliotek
# (venv-y w Debianie, .local, .cache, pip).

PRZED="${PRZED:-2026-10-25 08:40}"
TRYB="${1:-pokaz}"
H="$HOME"
T="${PREFIX:-/data/data/com.termux/files/usr}/tmp"

stare() { [ -e "$1" ] || [ -L "$1" ] || return 1; [ -z "$(find "$1" -maxdepth 0 ! -newermt "$PRZED" 2>/dev/null)" ] && return 1; return 0; }
zrob() {
    if [ "$TRYB" = "usun" ]; then rm -rf -- "$1" && echo "usunięto  $1"; else
        echo "do usunięcia  $1  ($(du -sh "$1" 2>/dev/null | cut -f1))"; fi
}

echo "== ~ (Termux) — śmieci drużyn spoza ich folderów"
shopt -s nullglob
for p in "$H"/agent_background_*.log "$H"/install_tf_debian.log "$H"/proot_install.log \
         "$H"/ubuntu_pip.log "$H"/ubuntu_torch.log "$H"/debian_torch*.log "$H"/distill_run.log \
         "$H"/rrp_err.txt "$H"/sprawdz_*.py "$H"/sprawdz_*_diag.log "$H"/sandbox_v2.py \
         "$H"/proposer_cv_v3.py "$H"/baudrillard_predict.py "$H"/fake_labeled.csv \
         "$H"/src "$H"/runs "$H"/logs "$H"/projekt "$H"/archiwum_rywalizacji \
         "$H"/rsi.bak.faza11.* "$H"/FINAL_OK.txt "$H"/__pycache__; do
    stare "$p" && zrob "$p"
done

echo
echo "== tmp Termuksa ($T) — pliki tymczasowe drużyn"
for n in sogo_err.txt spr.out spr.err e.txt torchdl fake_labeled.csv rrp_err.txt notka.md \
         rung2_quick.log ogm.txt potop.txt lalka_g.txt lalka.txt distill_run.log t.out t.err \
         v7_hash_before.txt wynik_hash_before.txt torch_test maml_sanity.json r5.out r5.err \
         d.out d.err per_fold.py x baselines.md marker recon.out score_err.txt; do
    stare "$T/$n" && zrob "$T/$n"
done
for p in "$T"/pip-unpack-* "$T"/tmp*.sh; do stare "$p" && zrob "$p"; done

echo
echo "== Debian: /root i /tmp (od środka, przez proot-distro)"
if command -v proot-distro >/dev/null 2>&1; then
    proot-distro login debian -- env PRZED="$PRZED" TRYB="$TRYB" bash -c '
        stare() { [ -e "$1" ] || return 1; [ -n "$(find "$1" -maxdepth 0 ! -newermt "$PRZED" 2>/dev/null)" ]; }
        zrob() { if [ "$TRYB" = "usun" ]; then rm -rf -- "$1" && echo "usunięto  (Debian) $1"; else
                 echo "do usunięcia  (Debian) $1  ($(du -sh "$1" 2>/dev/null | cut -f1))"; fi; }
        cd /root 2>/dev/null && for p in *; do
            [ -e "$p" ] || continue
            [ -f "$p/pyvenv.cfg" ] && { echo "zostaje (biblioteki) /root/$p"; continue; }
            stare "/root/$p" && zrob "/root/$p"
        done
        for n in sogo_err.txt spr.out spr.err e.txt torchdl fake_labeled.csv rrp_err.txt notka.md \
                 rung2_quick.log ogm.txt potop.txt lalka_g.txt lalka.txt distill_run.log t.out t.err \
                 v7_hash_before.txt wynik_hash_before.txt torch_test maml_sanity.json r5.out r5.err \
                 d.out d.err per_fold.py x baselines.md marker recon.out score_err.txt; do
            stare "/tmp/$n" && zrob "/tmp/$n"
        done
    ' 2>/dev/null || echo "(Debian się nie otworzył — ta część pominięta)"
fi

echo
echo "== Starsze rzeczy z wcześniejszych celów (sprzed rywalizacji) — tylko pokazuję, nie usuwam:"
for p in "$H"/rsi "$H"/zalazek_AI "$H"/vosk-model-small-pl-0.22 "$H"/monitor_agent; do
    [ -e "$p" ] && echo "  $p  ($(du -sh "$p" 2>/dev/null | cut -f1)) — usuń sam: rm -rf $p"
done

[ "$TRYB" = "usun" ] || { echo; echo "Nic nie usunąłem. Żeby usunąć: bash $0 usun"; }
