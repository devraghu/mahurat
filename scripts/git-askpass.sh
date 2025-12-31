#!/bin/sh
# Helper used via GIT_ASKPASS to supply credentials when pushing via PAT.
case "$1" in
  *Username*) printf 'x-access-token\n' ;;
  *Password*) printf '%s\n' "$GITHUB_PAT" ;;
  *) printf '%s\n' "$GITHUB_PAT" ;;
esac
