export LANG=${LANG:-C.UTF-8}
# phone consoles: run in tmux for volume-key scrollback (shell/tmux-console.conf); one session per tty
case $(tty) in /dev/tty[1-4]) [ -z "$TMUX" ] && command -v tmux >/dev/null &&
    exec tmux -L console -f ~/.config/tmux/console.conf new-session -A -s "$(basename "$(tty)")" ;; esac
[ -n "$BASH_VERSION" ] && [ -r ~/.bashrc ] && . ~/.bashrc
export PATH=$HOME/.local/bin:$PATH
