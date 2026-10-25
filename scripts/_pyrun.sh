#!/usr/bin/env bash
# Cross-platform Python launcher for AI log hooks.
# Tries python3 → python → py -3 on PATH; on Windows, falls back to common
# Python install locations because Git Bash launched by some hooks gets a
# stripped PATH that omits the Windows Python directory.
# Designed to be sourced or called as: bash scripts/_pyrun.sh <script> [args...]
#
# Exits 0 silently if no Python is found — hooks must never block the AI tool.
#
# A candidate counts only if it *runs* Python, which is not the same question as
# whether it exists. Windows installs an "App Execution Alias" at
# `AppData/Local/Microsoft/WindowsApps/python3.exe`: a real, executable file on
# PATH that prints an advert for the Microsoft Store and exits 49. `command -v`
# finds it and is satisfied, so the launcher this replaced picked the stub and
# never fell through to the working `python` beside it — and because every
# caller here ends in `|| true` (a hook must not block a push), the failure was
# completely silent. That is how a whole session of Claude Code hooks plus the
# pre-push sweep and upload wrote nothing at all while every command "succeeded".
#
# The probe costs one extra interpreter start-up per candidate, on a hook that
# fires per tool call. That is the right trade: a launcher that is fast and
# picks the wrong interpreter produces an empty log nobody notices until grading.
set -u

#: Whether "$@" is an interpreter that can actually execute Python.
_runs_python() {
  "$@" -c "import sys" >/dev/null 2>&1
}

PY=()
for cand in python3 python; do
  if _runs_python "$cand"; then
    PY=("$cand")
    break
  fi
done

if [ ${#PY[@]} -eq 0 ] && _runs_python py -3; then
  PY=(py -3)
fi

if [ ${#PY[@]} -eq 0 ]; then
  # PATH lookup failed entirely — probe standard Windows install locations.
  shopt -s nullglob 2>/dev/null || true
  for cand in \
    /c/Users/*/AppData/Local/Programs/Python/Python*/python.exe \
    "/c/Program Files/Python"*/python.exe \
    "/c/Program Files (x86)/Python"*/python.exe \
    /c/Python*/python.exe; do
    if _runs_python "$cand"; then
      PY=("$cand")
      break
    fi
  done
  shopt -u nullglob 2>/dev/null || true
fi

[ ${#PY[@]} -gt 0 ] || exit 0

exec "${PY[@]}" "$@"
