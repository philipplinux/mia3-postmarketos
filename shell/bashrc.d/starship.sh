# starship prompt (Nerd Font icons) — sourced via ~/.bashrc.d
if command -v starship >/dev/null 2>&1; then
    eval "$(starship init bash)"
else
    # fallback if ~/.local/bin not on PATH yet in this shell
    if [ -x "$HOME/.local/bin/starship" ]; then
        eval "$($HOME/.local/bin/starship init bash)"
    fi
fi