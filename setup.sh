#!/usr/bin/env bash
# Installs MeetRec, or updates an existing install, in one run. Paste into
# Terminal:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/frasal-test/meetrec/main/setup.sh)"
#
# or run ./setup.sh from a copy of the project. Safe to repeat: every step
# checks what is already in place and skips it. INSTALL.md describes the same
# steps by hand.
set -euo pipefail

REPO_URL="${MEETREC_REPO:-https://github.com/frasal-test/meetrec.git}"
SIGN_IDENTITY="${MEETREC_SIGN_IDENTITY:-MeetRec Dev}"
LABEL="com.frasal.meetrec"
CONFIG_PATH="$HOME/.config/meetrec/config.json"
# Kept in step with PYANNOTE_GATED_REPOS in meeting_recorder/control.py.
GATED_REPOS=(
    pyannote/segmentation-3.0
    pyannote/speaker-diarization-3.1
    pyannote/speaker-diarization-community-1
)

# ---------------------------------------------------------------- output

if [[ "$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p')" == *'"it'* ]]; then
    ITALIAN=1
else
    ITALIAN=0
fi

if [[ -t 1 ]]; then
    BOLD=$'\033[1m' GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' RESET=$'\033[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

# Every message comes in English and Italian; the Mac's first language picks.
text() {
    if ((ITALIAN)); then printf '%s' "$2"; else printf '%s' "$1"; fi
}

say() { printf '%s\n' "$(text "$1" "$2")"; }
step() { printf '\n%s== %s ==%s\n' "$BOLD" "$(text "$1" "$2")" "$RESET"; }
ok() { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$(text "$1" "$2")"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$(text "$1" "$2")"; }
fail() {
    printf '%s✗ %s%s\n' "$RED" "$(text "$1" "$2")" "$RESET" >&2
    exit 1
}

# Reads from the terminal even when the script itself arrived on stdin.
ask() {
    local reply
    printf '%s ' "$(text "$1" "$2")" >/dev/tty
    IFS= read -r reply </dev/tty
    printf '%s' "$reply"
}

ask_secret() {
    local reply
    printf '%s ' "$(text "$1" "$2")" >/dev/tty
    IFS= read -rs reply </dev/tty
    printf '\n' >/dev/tty
    printf '%s' "$reply"
}

wait_for_enter() {
    ask "Press Enter when done." "Premi Invio quando hai fatto." >/dev/null
}

# ---------------------------------------------------------------- checks

check_mac() {
    [[ "$(uname -s)" == "Darwin" ]] \
        || fail "MeetRec runs on macOS only." "MeetRec funziona solo su macOS."
    local version major
    version="$(sw_vers -productVersion)"
    major="${version%%.*}"
    if ((major < 13)); then
        fail "macOS $version is too old: MeetRec needs macOS 13 or later. Update from System Settings → General → Software Update." \
            "macOS $version è troppo vecchio: MeetRec richiede macOS 13 o successivo. Aggiornalo da Impostazioni di Sistema → Generali → Aggiornamento software."
    fi
    if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == "1" ]]; then
        ARCH="arm64"
        if [[ "$(uname -m)" != "arm64" ]]; then
            fail "This Terminal runs under Rosetta. Open a Terminal without \"Open using Rosetta\" and try again." \
                "Questo Terminale gira sotto Rosetta. Apri un Terminale senza \"Apri con Rosetta\" e riprova."
        fi
    else
        ARCH="x86_64"
    fi
    ok "macOS $version ($ARCH)" "macOS $version ($ARCH)"
}

command_line_tools_ready() {
    xcode-select -p >/dev/null 2>&1 \
        && swiftc --version >/dev/null 2>&1 \
        && git --version >/dev/null 2>&1
}

ensure_command_line_tools() {
    if command_line_tools_ready; then
        ok "Command Line Tools are installed" "Command Line Tools già installati"
        return
    fi
    if xcode-select -p >/dev/null 2>&1; then
        # Installed but unusable: usually the full Xcode app with its licence
        # not yet accepted, or xcode-select pointing at a removed copy.
        fail "The Command Line Tools are installed but swiftc does not work. Run these two commands (they ask for your Mac password), then run setup again:
    sudo xcode-select --switch /Applications/Xcode.app
    sudo xcodebuild -license accept" \
            "I Command Line Tools sono installati ma swiftc non funziona. Esegui questi due comandi (chiedono la password del Mac), poi rilancia l'installazione:
    sudo xcode-select --switch /Applications/Xcode.app
    sudo xcodebuild -license accept"
    fi
    step "Apple's Command Line Tools" "Command Line Tools di Apple"
    say "A macOS window opens: click Install and accept the licence. The download is several GB and can take a while; this script waits for it." \
        "Si apre una finestra di macOS: clicca Installa e accetta la licenza. Il download è di diversi GB e può richiedere tempo; questo script lo aspetta."
    xcode-select --install >/dev/null 2>&1 || true
    local waited=0
    until command_line_tools_ready; do
        sleep 10
        waited=$((waited + 10))
        if ((waited % 60 == 0)); then
            say "  …still waiting ($((waited / 60)) min)" "  …sto ancora aspettando ($((waited / 60)) min)"
        fi
    done
    ok "Command Line Tools installed" "Command Line Tools installati"
}

# ---------------------------------------------------------------- project

# When the script runs from a file inside the project, that copy is the one
# to set up. Otherwise (pasted into Terminal) it fetches the project and
# hands over to the setup.sh inside it, so the steps below always match the
# code they install.
locate_project() {
    local source="${BASH_SOURCE[0]:-}"
    if [[ -n "$source" && -f "$source" ]]; then
        local dir
        dir="$(cd "$(dirname "$source")" && pwd)"
        if [[ -f "$dir/meetrec.sh" ]]; then
            PROJECT_DIR="$dir"
            return
        fi
    fi
    PROJECT_DIR="${MEETREC_DIR:-$HOME/meetrec}"
    fetch_project "$PROJECT_DIR"
    exec env MEETREC_SETUP_HANDOVER=1 bash "$PROJECT_DIR/setup.sh"
}

fetch_project() {
    local dir="$1"
    step "Downloading MeetRec" "Download di MeetRec"
    if [[ -d "$dir/.git" && -f "$dir/meetrec.sh" ]]; then
        if git -C "$dir" pull --ff-only; then
            ok "Updated $dir" "Aggiornato $dir"
        else
            warn "Could not update $dir (local changes?). Continuing with the copy that is there." \
                "Non riesco ad aggiornare $dir (modifiche locali?). Proseguo con la copia presente."
        fi
    elif [[ -e "$dir" ]]; then
        fail "$dir already exists and is not a copy of MeetRec. Move it away, or choose another folder with MEETREC_DIR=/path/to/folder." \
            "$dir esiste già e non è una copia di MeetRec. Spostala, oppure scegli un'altra cartella con MEETREC_DIR=/percorso/cartella."
    else
        git clone "$REPO_URL" "$dir"
        ok "Downloaded into $dir" "Scaricato in $dir"
    fi
}

# ---------------------------------------------------------------- Hugging Face

open_hugging_face_pages() {
    say "I am opening the Hugging Face pages in your browser. In order:
  1. Create a free account (or sign in).
  2. On each of the three pyannote pages, fill in the short form and click the button to agree. You must be signed in to see it.
  3. On the tokens page, click \"Create new token\", choose the Read type, create it and copy it (it starts with hf_)." \
        "Apro le pagine di Hugging Face nel browser. Nell'ordine:
  1. Crea un account gratuito (o accedi).
  2. In ognuna delle tre pagine pyannote compila il breve modulo e clicca il pulsante per accettare. Devi aver fatto l'accesso per vederlo.
  3. Nella pagina dei token clicca \"Create new token\", scegli il tipo Read, crealo e copialo (inizia con hf_)."
    open "https://huggingface.co/join" || true
    local repo
    for repo in "${GATED_REPOS[@]}"; do
        open "https://huggingface.co/$repo" || true
    done
    open "https://huggingface.co/settings/tokens" || true
}

# GET with the token on stdin, so it never shows up in the process list.
hf_status() {
    printf 'Authorization: Bearer %s\n' "$1" \
        | curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H @- "$2" \
        || printf '000'
}

# 0: the token reads all three models. 1: it does not, and the message says
# why. 2: Hugging Face could not be reached.
check_token() {
    local token="$1" status repo
    local missing=()
    status="$(hf_status "$token" "https://huggingface.co/api/whoami-v2")"
    case "$status" in
        200) ;;
        401)
            warn "Hugging Face rejected this token: it is mistyped, revoked or incomplete." \
                "Hugging Face rifiuta questo token: è sbagliato, revocato o incompleto."
            return 1
            ;;
        *)
            return 2
            ;;
    esac
    for repo in "${GATED_REPOS[@]}"; do
        status="$(hf_status "$token" "https://huggingface.co/api/models/$repo/auth-check")"
        case "$status" in
            200) ;;
            401 | 403) missing+=("$repo") ;;
            *) return 2 ;;
        esac
    done
    if ((${#missing[@]})); then
        warn "The token works, but its account cannot read these models yet. Open each page while signed in to that same account and accept the conditions:" \
            "Il token funziona, ma il suo account non può ancora leggere questi modelli. Apri ogni pagina con l'accesso fatto a quello stesso account e accetta le condizioni:"
        for repo in "${missing[@]}"; do
            printf '    https://huggingface.co/%s\n' "$repo"
            open "https://huggingface.co/$repo" || true
        done
        say "(If you made a fine-grained token, it also needs \"Read access to contents of all public gated repos you can access\"; a plain Read token is simpler.)" \
            "(Se hai creato un token fine-grained, serve anche \"Read access to contents of all public gated repos you can access\"; un token Read normale è più semplice.)"
        return 1
    fi
    return 0
}

saved_token() {
    [[ -f "$PROJECT_DIR/.env" ]] || return 0
    sed -n 's/^HF_TOKEN=//p' "$PROJECT_DIR/.env" | tail -n 1 | tr -d "\"' "
}

# Asks for a token until one passes; an empty answer gives up. Sets TOKEN.
read_token() {
    local token result
    while true; do
        token="$(ask_secret "$1" "$2")"
        token="$(printf '%s' "$token" | tr -d '[:space:]')"
        [[ -n "$token" ]] || return 1
        if [[ "$token" != hf_* ]]; then
            warn "That is not a Hugging Face token: those start with hf_." \
                "Questo non è un token di Hugging Face: quelli iniziano con hf_."
            continue
        fi
        result=0
        check_token "$token" || result=$?
        if ((result == 0)); then
            ok "Token accepted: it can read all three models" "Token valido: legge tutti e tre i modelli"
            TOKEN="$token"
            return 0
        fi
        if ((result == 2)); then
            warn "Could not reach Hugging Face to check the token. Saving it anyway; ./meetrec.sh doctor checks it later." \
                "Non riesco a raggiungere Hugging Face per controllare il token. Lo salvo comunque; ./meetrec.sh doctor lo controlla dopo."
            TOKEN="$token"
            return 0
        fi
        say "Fix it in the browser, then paste the token again." \
            "Sistema nel browser, poi incolla di nuovo il token."
    done
}

# Speaker separation needs a token whose account accepted three sets of
# conditions, which takes a few minutes in the browser. Ask before the long
# downloads, so that work happens while the packages install rather than with
# setup stopped and waiting. Sets SPEAKERS to on, later or off.
choose_speaker_separation() {
    TOKEN=""
    step "Telling remote participants apart" "Distinguere i partecipanti remoti"
    if [[ "$ARCH" != "arm64" ]]; then
        say "This is an Intel Mac, where speaker separation is not available. Transcripts label your voice ME and everyone else REMOTE." \
            "Questo è un Mac Intel, dove la separazione dei parlanti non è disponibile. Le trascrizioni etichettano la tua voce ME e tutti gli altri REMOTE."
        SPEAKERS="off"
        return
    fi
    local existing result=0
    existing="$(saved_token)"
    if [[ -n "$existing" ]]; then
        check_token "$existing" || result=$?
        if ((result != 1)); then
            ok "The saved Hugging Face token works" "Il token di Hugging Face salvato funziona"
            TOKEN="$existing"
            SPEAKERS="on"
            return
        fi
        say "The token saved in .env needs attention (see above)." \
            "Il token salvato in .env va sistemato (vedi sopra)."
    fi
    say "MeetRec can tell remote participants apart (REMOTE_01, REMOTE_02, …). That needs a free Hugging Face account and a token.
  1) I have the token: paste it now
  2) Not yet: open the pages and I will do it while MeetRec installs (about 10 minutes)
  3) Skip: every remote participant is labelled REMOTE (run setup again later to add it)" \
        "MeetRec può distinguere i partecipanti remoti (REMOTE_01, REMOTE_02, …). Serve un account gratuito su Hugging Face e un token.
  1) Ho il token: lo incollo ora
  2) Non ancora: apri le pagine e lo faccio mentre MeetRec si installa (circa 10 minuti)
  3) Salta: tutti i partecipanti remoti diventano REMOTE (rilancia l'installazione più tardi per aggiungerlo)"
    local choice
    while true; do
        choice="$(ask "Choose 1, 2 or 3:" "Scegli 1, 2 o 3:")"
        case "$choice" in
            1)
                if read_token "Paste the token (it is not shown), or press Enter to do it later:" \
                    "Incolla il token (non viene mostrato), oppure premi Invio per farlo dopo:"; then
                    SPEAKERS="on"
                else
                    open_hugging_face_pages
                    SPEAKERS="later"
                fi
                return
                ;;
            2)
                open_hugging_face_pages
                SPEAKERS="later"
                return
                ;;
            3)
                SPEAKERS="off"
                return
                ;;
        esac
    done
}

collect_late_token() {
    [[ "$SPEAKERS" == "later" ]] || return 0
    step "Hugging Face token" "Token di Hugging Face"
    if read_token "Paste the token (it is not shown), or press Enter to continue without speaker separation:" \
        "Incolla il token (non viene mostrato), oppure premi Invio per continuare senza separazione dei parlanti:"; then
        SPEAKERS="on"
    else
        SPEAKERS="off"
        say "Continuing without speaker separation. Run setup again once you have the token." \
            "Continuo senza separazione dei parlanti. Rilancia l'installazione quando hai il token."
    fi
}

save_token() {
    local env_file="$PROJECT_DIR/.env" temporary
    temporary="$(mktemp "$PROJECT_DIR/.env.XXXXXX")"
    if [[ -f "$env_file" ]]; then
        grep -v '^HF_TOKEN=' "$env_file" >"$temporary" || true
    fi
    printf 'HF_TOKEN=%s\n' "$1" >>"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$env_file"
}

# Writes transcription.diarize_system only when it differs from what the
# config already says, so a fresh install with the default needs no file.
set_speaker_separation() {
    "$PROJECT_DIR/.venv/bin/python" - "$CONFIG_PATH" "$1" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
wanted = sys.argv[2] == "on"
raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
transcription = raw.setdefault("transcription", {})
if transcription.get("diarize_system", True) == wanted:
    sys.exit(0)
transcription["diarize_system"] = wanted
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(f".{path.name}.tmp")
temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
}

apply_speaker_choice() {
    if [[ "$SPEAKERS" == "on" ]]; then
        save_token "$TOKEN"
        set_speaker_separation on
        ok "Speaker separation is on" "Separazione dei parlanti attiva"
    else
        set_speaker_separation off
        ok "Speaker separation is off" "Separazione dei parlanti disattivata"
    fi
}

# ---------------------------------------------------------------- Python

# A Python MeetRec can use: 3.10 to 3.13, built for this Mac's processor.
python_fits() {
    local found
    found="$("$1" -c 'import platform, sys; print(platform.machine(), "%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
        || return 1
    [[ "${found% *}" == "$ARCH" ]] || return 1
    case "${found#* }" in
        3.10 | 3.11 | 3.12 | 3.13) return 0 ;;
    esac
    return 1
}

find_python() {
    local candidate path
    for candidate in python3.12 python3.13 python3.11 python3.10; do
        path="$(command -v "$candidate" 2>/dev/null)" || continue
        if python_fits "$path"; then
            printf '%s' "$path"
            return 0
        fi
    done
    return 1
}

# uv downloads a standalone Python into the home folder: no Homebrew, no
# installer, no password.
python_from_uv() {
    local uv path
    uv="$(command -v uv 2>/dev/null || printf '%s' "$HOME/.local/bin/uv")"
    if [[ ! -x "$uv" ]]; then
        say "Downloading uv, which fetches Python for MeetRec…" \
            "Scarico uv, che procura Python per MeetRec…"
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
        uv="$HOME/.local/bin/uv"
    fi
    "$uv" python install 3.12 >&2
    path="$("$uv" python find --managed-python 3.12 2>/dev/null || "$uv" python find 3.12)"
    python_fits "$path" || return 1
    printf '%s' "$path"
}

ensure_python_environment() {
    step "Python environment" "Ambiente Python"
    local venv_python="$PROJECT_DIR/.venv/bin/python"
    if [[ -x "$venv_python" ]] && python_fits "$venv_python"; then
        ok "Using the existing .venv" "Uso il .venv esistente"
    else
        if [[ -e "$PROJECT_DIR/.venv" ]]; then
            warn "The existing .venv uses an unsuitable Python; replacing it." \
                "Il .venv esistente usa un Python non adatto; lo sostituisco."
            rm -rf "$PROJECT_DIR/.venv"
        fi
        local python
        python="$(find_python)" || python="$(python_from_uv)" \
            || fail "Could not find or install Python 3.12. See Step 3 of INSTALL.md." \
                "Non riesco a trovare o installare Python 3.12. Vedi lo Step 3 di INSTALL.md."
        "$python" -m venv "$PROJECT_DIR/.venv"
        ok "Created .venv with $("$venv_python" --version)" "Creato .venv con $("$venv_python" --version)"
    fi
}

install_packages() {
    local requirements="requirements.txt"
    [[ "$ARCH" == "arm64" ]] && requirements="requirements-diarization.txt"
    step "Python packages" "Pacchetti Python"
    say "Installing $requirements. The first time this downloads about 1 GB and takes several minutes." \
        "Installo $requirements. La prima volta scarica circa 1 GB e richiede diversi minuti."
    "$PROJECT_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
    "$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade -r "$PROJECT_DIR/$requirements"
    ok "Python packages installed" "Pacchetti Python installati"
}

# ---------------------------------------------------------------- signing

# Writes a self-signed code-signing certificate, its key and a .p12 bundle of
# both into $1. Same result as Keychain Access → Certificate Assistant, valid
# ten years. /usr/bin/openssl (LibreSSL) writes the .p12 in the format the
# keychain imports; a Homebrew OpenSSL 3 would not.
make_signing_identity() {
    local dir="$1" name="$2" password="$3"
    cat >"$dir/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $name
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
EOF
    /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -config "$dir/openssl.cnf" \
        -keyout "$dir/key.pem" -out "$dir/cert.pem" 2>/dev/null
    /usr/bin/openssl pkcs12 -export -name "$name" \
        -inkey "$dir/key.pem" -in "$dir/cert.pem" \
        -out "$dir/identity.p12" -passout "pass:$password"
}

signing_identity_valid() {
    security find-identity -v -p codesigning 2>/dev/null | grep -qF "\"$SIGN_IDENTITY\""
}

ensure_signing_certificate() {
    step "Signing certificate" "Certificato di firma"
    if signing_identity_valid; then
        ok "Certificate \"$SIGN_IDENTITY\" found" "Certificato \"$SIGN_IDENTITY\" trovato"
        return
    fi
    say "Creating the \"$SIGN_IDENTITY\" certificate, so macOS remembers MeetRec's permissions across updates. macOS asks for your Mac password once to trust it." \
        "Creo il certificato \"$SIGN_IDENTITY\", così macOS ricorda i permessi di MeetRec anche dopo gli aggiornamenti. macOS chiede una volta la password del Mac per considerarlo affidabile."
    local dir keychain password
    dir="$(mktemp -d)"
    keychain="$HOME/Library/Keychains/login.keychain-db"
    if security find-certificate -c "$SIGN_IDENTITY" "$keychain" >/dev/null 2>&1; then
        # Created earlier but never trusted: trust that one.
        security find-certificate -c "$SIGN_IDENTITY" -p "$keychain" >"$dir/cert.pem"
    else
        password="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
        make_signing_identity "$dir" "$SIGN_IDENTITY" "$password"
        security import "$dir/identity.p12" -k "$keychain" -P "$password" \
            -T /usr/bin/codesign >/dev/null
    fi
    security add-trusted-cert -r trustRoot -p codeSign -k "$keychain" "$dir/cert.pem" || true
    rm -rf "$dir"
    if signing_identity_valid; then
        ok "Certificate \"$SIGN_IDENTITY\" created" "Certificato \"$SIGN_IDENTITY\" creato"
    else
        warn "The certificate is not trusted yet. MeetRec still works, but macOS will ask for permissions again after each update. Step 7 of INSTALL.md fixes it by hand." \
            "Il certificato non è ancora considerato affidabile. MeetRec funziona lo stesso, ma macOS richiederà i permessi dopo ogni aggiornamento. Lo Step 7 di INSTALL.md lo sistema a mano."
    fi
}

# ---------------------------------------------------------------- app

build_and_install() {
    step "Building MeetRec" "Compilazione di MeetRec"
    if signing_identity_valid; then
        say "If macOS says \"codesign wants to sign using key\", type your Mac password and click Always Allow (it may ask twice)." \
            "Se macOS dice \"codesign vuole firmare usando la chiave\", scrivi la password del Mac e clicca Consenti sempre (può chiederlo due volte)."
    fi
    "$PROJECT_DIR/meetrec.sh" install
}

newest_session_since() {
    local recordings="$1" marker="$2"
    find "$recordings" -mindepth 1 -maxdepth 1 -type d -newer "$marker" 2>/dev/null \
        | sort | tail -n 1
}

guide_permissions() {
    step "macOS permissions" "Permessi di macOS"
    local answer
    answer="$(ask "Has MeetRec already recorded successfully on this Mac? [y/N]" \
        "MeetRec ha già registrato correttamente su questo Mac? [s/N]")"
    case "$answer" in
        [yYsS]*)
            ok "Skipping the permissions" "Salto i permessi"
            return
            ;;
    esac
    local recordings marker session
    recordings="$("$PROJECT_DIR/.venv/bin/python" -m meeting_recorder.control recordings-dir)"
    marker="$(mktemp)"
    say "macOS asks for permissions only when MeetRec first tries to record, and the first try is expected to fail.
  1. Click the MeetRec icon in the menu bar (a microphone between sound bars) and choose Start recording — Italiano.
  2. MeetRec asks whether you told the participants: click the first button.
  3. macOS asks for the Microphone: click Allow.
  4. macOS asks about screen recording: click Open System Settings. MeetRec then says \"Recording failed\": that is expected." \
        "macOS chiede i permessi solo quando MeetRec prova a registrare per la prima volta, e il primo tentativo fallisce di sicuro.
  1. Clicca l'icona di MeetRec nella barra dei menu (un microfono tra barre sonore) e scegli Start recording — Italiano.
  2. MeetRec chiede se hai informato i partecipanti: clicca il primo pulsante.
  3. macOS chiede il Microfono: clicca Consenti.
  4. macOS chiede la registrazione dello schermo: clicca Apri Impostazioni di Sistema. MeetRec poi dice \"Recording failed\": è normale."
    wait_for_enter
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" || true
    say "In the System Settings window that opened, switch MeetRec on under \"Screen & System Audio Recording\" (\"Screen Recording\" on macOS 13 and 14). If macOS offers to quit and reopen it, choose Later." \
        "Nella finestra di Impostazioni di Sistema che si è aperta, attiva MeetRec sotto \"Registrazione schermo e audio di sistema\" (\"Registrazione schermo\" su macOS 13 e 14). Se macOS propone di chiuderlo e riaprirlo, scegli Più tardi."
    wait_for_enter
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    ok "MeetRec restarted with the new permission" "MeetRec riavviato con il nuovo permesso"
    while true; do
        touch "$marker"
        say "Now a real test: choose Start recording again, play a few seconds of any video and say a few words, then choose Stop recording." \
            "Ora una prova vera: scegli di nuovo Start recording, fai partire qualche secondo di un video qualsiasi e di' qualche parola, poi scegli Stop recording."
        wait_for_enter
        session="$(newest_session_since "$recordings" "$marker")"
        if [[ -n "$session" && -s "$session/audio/mic.caf" && -s "$session/audio/system.caf" ]]; then
            ok "Both tracks recorded: $session" "Entrambe le tracce registrate: $session"
            say "Its transcription is starting. The first one downloads the transcription model (about 1.5 GB), so it takes longer." \
                "La sua trascrizione sta partendo. La prima scarica il modello di trascrizione (circa 1,5 GB), quindi richiede più tempo."
            break
        fi
        if [[ -z "$session" ]]; then
            warn "No new recording found." "Non trovo nessuna nuova registrazione."
        else
            [[ -s "$session/audio/mic.caf" ]] || warn "Your voice was not recorded: check Microphone in System Settings → Privacy & Security." \
                "La tua voce non è stata registrata: controlla Microfono in Impostazioni di Sistema → Privacy e sicurezza."
            [[ -s "$session/audio/system.caf" ]] || warn "The other side was not recorded: check that MeetRec is switched on under Screen & System Audio Recording." \
                "L'altra parte non è stata registrata: controlla che MeetRec sia attivo sotto Registrazione schermo e audio di sistema."
            launchctl kickstart -k "gui/$(id -u)/$LABEL"
        fi
        answer="$(ask "Press Enter to try again, or type s to skip:" "Premi Invio per riprovare, oppure scrivi s per saltare:")"
        [[ "$answer" == [sS]* ]] && break
    done
    rm -f "$marker"
}

finish() {
    step "Final check" "Controllo finale"
    local status=0
    "$PROJECT_DIR/meetrec.sh" doctor || status=$?
    printf '\n'
    if ((status == 0)); then
        ok "MeetRec is ready. Use it from its icon in the menu bar." \
            "MeetRec è pronto. Si usa dalla sua icona nella barra dei menu."
    else
        warn "Some check above shows ✗. INSTALL.md, section Troubleshooting, explains each one; then run setup again." \
            "Qualche controllo qui sopra mostra ✗. INSTALL.md, sezione Troubleshooting, li spiega uno per uno; poi rilancia l'installazione."
    fi
    say "Wear headphones during meetings, or the other participants are transcribed twice.
To update MeetRec later, run the same command again." \
        "Durante le riunioni usa le cuffie, altrimenti gli altri partecipanti vengono trascritti due volte.
Per aggiornare MeetRec in futuro, rilancia lo stesso comando."
    return "$status"
}

main() {
    if [[ -z "${MEETREC_SETUP_HANDOVER:-}" ]]; then
        printf '%sMeetRec setup%s\n' "$BOLD" "$RESET"
    fi
    check_mac
    ensure_command_line_tools
    locate_project
    cd "$PROJECT_DIR"
    choose_speaker_separation
    ensure_python_environment
    install_packages
    collect_late_token
    apply_speaker_choice
    ensure_signing_certificate
    build_and_install
    guide_permissions
    finish
}

# Sourcing the file (for tests) defines the functions without running setup.
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi
main "$@"
