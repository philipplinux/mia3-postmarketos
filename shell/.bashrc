# interactive bash: snippets in ~/.bashrc.d (same layout as the desktop)
[[ $- == *i* ]] || return
HISTSIZE=5000 HISTFILESIZE=10000 HISTCONTROL=ignoredups
# the framebuffer console (TERM=linux) has no Nerd Font glyphs: plain-text prompt there
[[ $TERM == linux ]] && export STARSHIP_CONFIG=~/.config/starship-plain.toml
if [ -d ~/.bashrc.d ]; then
    for rc in ~/.bashrc.d/*; do [ -r "$rc" ] && . "$rc"; done
    unset rc
fi
