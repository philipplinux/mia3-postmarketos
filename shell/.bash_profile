export LANG=${LANG:-C.UTF-8}
# tty5: sway GPU terminal (foot + wvkbd, sway/config)
[ "$(tty)" = /dev/tty5 ] && command -v sway >/dev/null && exec sway >/tmp/sway.log 2>&1
# phone consoles tty2-4: run in tmux for volume-key scrollback (shell/tmux-console.conf); one session per tty.
# tty1 stays plain so kernel messages (printk to the console) stay visible there.
case $(tty) in /dev/tty[2-4]) [ -z "$TMUX" ] && command -v tmux >/dev/null &&
    exec tmux -L console -f ~/.config/tmux/console.conf new-session -A -s "$(basename "$(tty)")" ;; esac
[ -n "$BASH_VERSION" ] && [ -r ~/.bashrc ] && . ~/.bashrc
export PATH=$HOME/.local/bin:$PATH
