export LANG=${LANG:-C.UTF-8}
# tty2: sway GPU terminal (foot + wvkbd, sway/config)
[ "$(tty)" = /dev/tty2 ] && command -v sway >/dev/null && exec sway >/tmp/sway.log 2>&1
[ -n "$BASH_VERSION" ] && [ -r ~/.bashrc ] && . ~/.bashrc
export PATH=$HOME/.local/bin:$PATH
