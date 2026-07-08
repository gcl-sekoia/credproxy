# Example profile's prompt tweak — an oh-my-zsh custom drop-in (sourced from
# $ZSH_CUSTOM alongside base's mise.zsh, no ~/.zshrc edit).
#
# The devcontainers theme shows the username (%n = "vscode", not useful here).
# Keep the theme but swap the username for the short hostname (%m = the workspace
# name). Custom files are sourced before the theme sets PROMPT, so defer the swap
# to precmd (runs before each prompt); it's idempotent (once %n is gone, no-op).
_host_not_user() { PROMPT="${PROMPT//\%n/%m}"; }
autoload -Uz add-zsh-hook
add-zsh-hook precmd _host_not_user
