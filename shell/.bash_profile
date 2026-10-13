export LANG=${LANG:-C.UTF-8}
# tty5: sway GPU terminal (foot + wvkbd, sway/config)
[ "$(tty)" = /dev/tty5 ] && command -v sway >/dev/null && exec sway >/tmp/sway.log 2>&1
[ -n "$BASH_VERSION" ] && [ -r ~/.bashrc ] && . ~/.bashrc
export PATH=$HOME/.local/bin:$PATH
