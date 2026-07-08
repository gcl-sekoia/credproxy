# Activate mise in interactive zsh so the toolchain pack's installed CLIs are on
# PATH. oh-my-zsh sources every *.zsh in $ZSH_CUSTOM (~/.oh-my-zsh/custom) at
# startup, so this extends the image's stock config with NO edit to ~/.zshrc.
# (Assumes the default devcontainer image's zsh + oh-my-zsh; drop this mount for
# a different shell/image.)
eval "$(mise activate zsh)"
