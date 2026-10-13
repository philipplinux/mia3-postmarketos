# ble.sh: Bash line editor, syntax highlighting, and menus.
# Loaded before fzf.sh by the .bashrc.d alphabetical order.
if [[ $- == *i* && -r "$HOME/.local/share/blesh/ble.sh" ]]; then
    source "$HOME/.local/share/blesh/ble.sh"
fi
