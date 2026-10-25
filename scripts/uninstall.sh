#!/usr/bin/env bash
# Removes Wednesday's runtime pieces. Asks before deleting your memories and model files.
# Never touches system packages, your Hyprland config (unless you installed our snippet, which
# it offers to remove), or anything outside Wednesday's own directories.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/wednesday"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/wednesday"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/wednesday"
ask() { read -r -p "  $1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

echo "Uninstalling Wednesday…"
[[ -x "$HERE/.venv/bin/python" ]] && PYTHONPATH="$HERE/src" "$HERE/.venv/bin/python" -m wednesday stop >/dev/null 2>&1
systemctl --user disable --now wednesday.service >/dev/null 2>&1 && echo "  removed systemd user service"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/wednesday.service"
[[ -L "$HOME/.local/bin/wednesday" ]] && rm "$HOME/.local/bin/wednesday" && echo "  removed ~/.local/bin/wednesday"
bash "$HERE/scripts/hyprland.sh" remove 2>/dev/null
rm -rf "$STATE" && echo "  removed logs/state ($STATE)"
rm -rf "$HERE/.venv" && echo "  removed Python env"
if [[ -d "$DATA" ]]; then
  ask "Delete downloaded models/runtime/voices ($(du -sh "$DATA/runtime" "$DATA/models" "$DATA/voices" 2>/dev/null | awk '{s=s" "$1} END{print s}'))?" \
    && rm -rf "$DATA/runtime" "$DATA/models" "$DATA/voices" && echo "  removed models"
  if [[ -f "$DATA/memory.db" ]]; then
    ask "Delete Wednesday's memories ($DATA/memory.db)? This cannot be undone." && rm -f "$DATA"/memory.db* && echo "  memories deleted"
  fi
  rmdir "$DATA" 2>/dev/null
fi
[[ -d "$CONF" ]] && ask "Delete your config overrides ($CONF)?" && rm -rf "$CONF"
echo "Done. The source code in $HERE is untouched — delete the folder if you want it gone too."
