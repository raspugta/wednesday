#!/usr/bin/env bash
# Read-only health check. Changes nothing.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/wednesday"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/wednesday"
pass() { printf "  \033[92m✓\033[0m %-22s %s\n" "$1" "$2"; }
fail() { printf "  \033[91m✗\033[0m %-22s %s\n" "$1" "$2"; RC=1; }
info() { printf "  \033[33m·\033[0m %-22s %s\n" "$1" "$2"; }
RC=0
printf "\033[1mWednesday doctor\033[0m\n"
pass "os" "$(. /etc/os-release; echo "$PRETTY_NAME") · kernel $(uname -r)"
pass "cpu / ram" "$(lscpu | sed -n 's/Model name: *//p') · $(free -h | awk '/Mem:/{print $2" total, "$7" available"}')"
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  pass "gpu" "$(nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total,temperature.gpu --format=csv,noheader)"
else fail "gpu" "nvidia-smi unavailable — CPU fallback only"; fi
[[ "$XDG_SESSION_TYPE" == wayland ]] && pass "session" "wayland ${HYPRLAND_INSTANCE_SIGNATURE:+(Hyprland $(hyprctl version -j 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag"])' 2>/dev/null))}" || info "session" "${XDG_SESSION_TYPE:-none} (avatar uses a normal window outside Wayland)"
python3 -c "import gi; gi.require_version('GtkLayerShell','0.1'); from gi.repository import GtkLayerShell" 2>/dev/null && pass "gtk-layer-shell" "available" || fail "gtk-layer-shell" "missing (sudo apt install gir1.2-gtklayershell-0.1)"
[[ -x "$HERE/.venv/bin/python" ]] && pass "venv" "$HERE/.venv" || fail "venv" "missing — run ./wednesday setup"
RT=$(ls -d "$DATA"/runtime/*/llama-*/ 2>/dev/null | head -1)
[[ -n "$RT" && -x "$RT/llama-server" ]] && pass "llama.cpp" "$("$RT/llama-server" --version 2>&1 | head -1)" || fail "llama.cpp" "missing — run ./wednesday setup"
if [[ -n "$RT" ]]; then
  dev=$(LD_LIBRARY_PATH="$RT" "$RT/llama-server" --list-devices 2>&1 | grep -i nvidia | head -1 | sed 's/^ *//')
  [[ -n "$dev" ]] && pass "gpu backend" "$dev" || info "gpu backend" "no NVIDIA Vulkan device — CPU inference"
fi
for m in "$DATA"/models/*.gguf; do [[ -f "$m" ]] && pass "model" "$(basename "$m") ($(du -h "$m" | cut -f1))"; done
ls "$DATA"/models/*.gguf >/dev/null 2>&1 || fail "model" "no .gguf in $DATA/models"
[[ -f "$DATA/voices/en_GB-cori-high.onnx" ]] && pass "tts voice" "en_GB-cori-high" || info "tts voice" "not installed (optional)"
[[ -d "$DATA/voices/whisper-base.en" ]] && pass "stt model" "whisper base.en" || info "stt model" "not installed (optional)"
command -v pw-record >/dev/null && pass "audio" "PipeWire $(pactl info 2>/dev/null | sed -n 's/Server Name: //p')" || info "audio" "pw-record missing — voice disabled"
OURS=$(cat "$STATE/llama-server.pid" 2>/dev/null)
for svc in ollama llama-server; do
  for pid in $(pgrep -x "$svc"); do [[ "$pid" != "$OURS" ]] && info "other runtime" "$svc (pid $pid) is running — it shares the GPU with Wednesday"; done
done
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:7377"; then pass "backend" "listening on 127.0.0.1:7377"; else info "backend" "not running (start: wednesday)"; fi
[[ -f "$STATE/token" ]] && [[ "$(stat -c %a "$STATE/token")" == 600 ]] && pass "api token" "0600" || info "api token" "created on first start"
[[ -f "$DATA/memory.db" ]] && pass "memory db" "$(python3 -c "import sqlite3;c=sqlite3.connect('file:$DATA/memory.db?mode=ro',uri=True);print(c.execute('pragma integrity_check').fetchone()[0], '·', c.execute('select count(*) from memories').fetchone()[0], 'items')" 2>/dev/null)" || info "memory db" "created on first start"
exit $RC
