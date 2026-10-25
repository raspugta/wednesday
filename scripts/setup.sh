#!/usr/bin/env bash
# Wednesday setup — idempotent, user-level (no sudo), asks before every large download.
#
#   ./wednesday setup            interactive
#   ./wednesday setup --yes      accept the default downloads (model ~1.3 GB, voice ~250 MB)
#   ./wednesday setup --no-voice skip voice packages and models
#   ./wednesday setup --model qwen35-2b-q8   fetch a different model profile
#
# Everything lands in:
#   <repo>/.venv                        isolated Python env (system Python untouched)
#   ~/.local/share/wednesday/runtime    llama.cpp (official ggml-org release, checksum-pinned)
#   ~/.local/share/wednesday/models     GGUF weights (checksum-pinned)
#   ~/.local/share/wednesday/voices     Piper voice + Whisper model
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/wednesday"
VENV="$HERE/.venv"
YES=0; VOICE=1; MODEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --no-voice) VOICE=0 ;;
    --voice) VOICE=1 ;;
    --model) MODEL="$2"; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac; shift
done

c() { printf "\033[96m%s\033[0m\n" "$*"; }
ok() { printf "  \033[92m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die() { printf "  \033[91m✗ %s\033[0m\n" "$*"; exit 1; }
ask() {  # ask "question" → 0 for yes
  [[ $YES == 1 ]] && return 0
  read -r -p "  $1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]
}
fetch() {  # fetch URL DEST SHA256 — download to .part, verify, then move into place
  local url="$1" dest="$2" sha="$3"
  if [[ -f "$dest" ]] && echo "$sha  $dest" | sha256sum -c --status; then ok "$(basename "$dest") (verified)"; return 0; fi
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 3 -C - --progress-bar -o "$dest.part" "$url" || die "download failed: $url"
  echo "$sha  $dest.part" | sha256sum -c --status || { rm -f "$dest.part"; die "checksum mismatch for $(basename "$dest") — refusing to use it"; }
  mv "$dest.part" "$dest"; ok "$(basename "$dest") downloaded and verified"
}

[[ $EUID -eq 0 ]] && die "Don't run Wednesday's setup as root."

c "◆ Wednesday setup"
# ---------------------------------------------------------------- system checks (read-only)
c "1/6 Checking the system"
missing=()
command -v python3 >/dev/null || missing+=(python3)
python3 -c "import venv" 2>/dev/null || missing+=(python3-venv)
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null || missing+=(python3-gi gir1.2-gtk-3.0)
python3 -c "import gi; gi.require_version('GtkLayerShell','0.1'); from gi.repository import GtkLayerShell" 2>/dev/null || missing+=(gir1.2-gtklayershell-0.1)
python3 -c "import cairo" 2>/dev/null || missing+=(python3-gi-cairo)
command -v curl >/dev/null || missing+=(curl)
command -v pw-record >/dev/null || warn "pw-record/pw-cat missing — voice will be disabled (package: pipewire-bin)"
if (( ${#missing[@]} )); then
  warn "missing system packages: ${missing[*]}"
  echo "    install them with:  sudo apt install ${missing[*]}"
  echo "    (Wednesday never runs sudo for you.)"
  [[ " ${missing[*]} " == *" python3-venv "* || " ${missing[*]} " == *" python3 "* ]] && exit 1
fi
command -v nvidia-smi >/dev/null && ok "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null)" || warn "no NVIDIA GPU detected — will run on CPU (slow)"
ok "Python $(python3 -V | cut -d' ' -f2), $(df -h "$HOME" | awk 'NR==2{print $4}') free in \$HOME"

# ---------------------------------------------------------------- python env
c "2/6 Python environment (.venv, isolated; uses system GTK bindings)"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv --system-site-packages "$VENV"
  ok "created $VENV"
else ok "venv exists"; fi
"$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null
EXTRAS="dev"; [[ $VOICE == 1 ]] && EXTRAS="dev,voice"
"$VENV/bin/python" -m pip install -q -e "$HERE[$EXTRAS]" && ok "installed wednesday[$EXTRAS]"

# ---------------------------------------------------------------- inference runtime
c "3/6 Inference runtime (llama.cpp b11853, Vulkan build, 35 MB)"
RT="$DATA/runtime/b11853"
if [[ -x "$RT/llama-b11853/llama-server" ]]; then ok "llama-server present"
else
  fetch "https://github.com/ggml-org/llama.cpp/releases/download/b11853/llama-b11853-bin-ubuntu-vulkan-x64.tar.gz" \
        "$DATA/runtime/llama-b11853-vulkan.tar.gz" 3f33cb08eea0e10b666fb7be3ed23f3ece9b7bc3a591a648d30f9825eab4eb05
  mkdir -p "$RT" && tar -xzf "$DATA/runtime/llama-b11853-vulkan.tar.gz" -C "$RT" && rm "$DATA/runtime/llama-b11853-vulkan.tar.gz"
  ok "extracted to $RT"
fi

# ---------------------------------------------------------------- model weights
c "4/6 Language model"
read -r MFILE MURL MSHA MSIZE < <(PYTHONPATH="$HERE/src" "$VENV/bin/python" - "$MODEL" <<'PY'
import sys
from wednesday.config import Config
cfg = Config.load()
name = sys.argv[1] or cfg.get("model.active")
p = cfg.get("model.profiles")[name]
print(p["file"], p["url"], p["sha256"], p["size_gb"])
PY
)
if [[ -f "$DATA/models/$MFILE" ]]; then
  echo "$MSHA  $DATA/models/$MFILE" | sha256sum -c --status && ok "$MFILE (verified)" || die "$MFILE exists but its checksum is wrong — delete it and rerun"
elif ask "Download $MFILE (${MSIZE} GB, Apache-2.0, from huggingface.co/unsloth)?"; then
  fetch "$MURL" "$DATA/models/$MFILE" "$MSHA"
else warn "skipped — Wednesday can't think without it (rerun setup later)"; fi

# ---------------------------------------------------------------- voice
c "5/6 Voice (optional, runs on CPU)"
if [[ $VOICE == 1 ]]; then
  V="$DATA/voices"
  if [[ -f "$V/en_GB-cori-high.onnx" && -d "$V/whisper-base.en" ]]; then ok "voice models present"
  elif ask "Download Piper voice 'cori' (114 MB, public domain) + Whisper base.en (141 MB, MIT)?"; then
    fetch "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/cori/high/en_GB-cori-high.onnx" \
          "$V/en_GB-cori-high.onnx" 470b4dd634c98f8a4850d7626ffc3dfc90774628eeef6605a6dd8f88f30a5903
    fetch "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/cori/high/en_GB-cori-high.onnx.json" \
          "$V/en_GB-cori-high.onnx.json" 9e7fb5b5671612c22f3c81cbe46c1ae87b031a4632bcb509e499dad6f1e2adec
    HF_HUB_DISABLE_TELEMETRY=1 "$VENV/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-whisper-base.en', revision='3d3d5dee26484f91867d81cb899cfcf72b96be6c',
                  local_dir='$V/whisper-base.en', allow_patterns=['*.json','*.bin','*.txt'])" >/dev/null && ok "whisper-base.en (pinned revision)"
  else warn "voice models skipped — text chat still works"; fi
else ok "voice skipped (--no-voice)"; fi

# ---------------------------------------------------------------- command + first-run warm-up
c "6/6 Finishing"
mkdir -p "$HOME/.local/bin"
if [[ -L "$HOME/.local/bin/wednesday" || ! -e "$HOME/.local/bin/wednesday" ]]; then
  ln -sfn "$HERE/wednesday" "$HOME/.local/bin/wednesday" && ok "command installed: ~/.local/bin/wednesday"
  [[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || warn "~/.local/bin is not on your PATH — add it, or run $HERE/wednesday"
else warn "~/.local/bin/wednesday exists and isn't ours — left untouched (run $HERE/wednesday)"; fi
mkdir -p "$HERE/workspace"
touch "$VENV/.wednesday-setup-ok"

if [[ -f "$DATA/models/$MFILE" && ! -f "$DATA/.shaders-warm" ]] && command -v nvidia-smi >/dev/null; then
  echo "  First GPU run compiles Vulkan shaders once (~20 s, GPU busy briefly)…"
  if PYTHONPATH="$HERE/src" timeout 180 "$VENV/bin/python" - <<'PY'
import asyncio
from wednesday.config import Config
from wednesday.core.event_bus import EventBus
from wednesday.core.model_manager import ModelManager
async def main():
    cfg = Config.load(); cfg.paths.ensure(); mm = ModelManager(cfg, EventBus())
    await mm.ensure_loaded()
    for n in (20, 300, 1800):   # exercise small, medium and large prompt batches
        async for _ in mm.stream_chat([{"role": "user", "content": "hi " * n}], max_tokens=1):
            pass
    await mm.close()
asyncio.run(main())
PY
  then touch "$DATA/.shaders-warm"; ok "GPU shaders compiled"; else warn "warm-up skipped (it will happen on first use instead)"; fi
fi
c "Done. Start her with:  wednesday      (or $HERE/wednesday)"
