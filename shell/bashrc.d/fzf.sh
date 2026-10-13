# fzf: fuzzy finder — files, history, cd via Ctrl-T / Ctrl-R / Alt-C
# (needs Nerd Font / the Hack NF Mono for the default spinner+glyphs)
# ble.sh has its own official integration, loaded from ~/.blerc.
# Do not also run `fzf --bash`: it would overwrite ble.sh's widgets.
if command -v fzf >/dev/null 2>&1 && [[ -z ${BLE_VERSION-} ]]; then
    eval "$(fzf --bash)"
fi

export FZF_DEFAULT_OPTS="--height 40% --layout=reverse --border --pointer='>' --marker='>'
    --preview-window=right:60%"
export FZF_DEFAULT_COMMAND='fd --type f 2>/dev/null || find . -type f'