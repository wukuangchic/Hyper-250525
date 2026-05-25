#!/bin/zsh
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export HYPER_ORDER_DIR="$PROJECT_DIR"
export ZDOTDIR="$PROJECT_DIR/.order-zsh"
exec /bin/zsh -i
