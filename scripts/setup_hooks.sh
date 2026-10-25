#!/usr/bin/env bash
# Install git pre-push hook for AI log submission (POSIX / Git Bash).
# Run once after cloning: bash scripts/setup_hooks.sh
set -e

HOOK_FILE=".git/hooks/pre-push"

# On Windows, System32 ships a WSL launcher also named bash.exe. Whatever
# spawns `git push` (VS Code's Source Control button, a GUI client, a
# stale-PATH shell...) doesn't reliably resolve a bare `bash` to Git's own
# bash ahead of that stub — when it wins, the hook fails outright with
# "Windows Subsystem for Linux has no installed distributions" and blocks
# every push through that client. A shebang with a *space* in the path
# ("C:/Program Files/Git/...") isn't parsed correctly either. The fix that
# survives both: resolve Git's own bash to its absolute 8.3 short path
# (no spaces, no PATH search) once, here at setup time, and bake it into
# the generated hook. On Mac/Linux there's no such stub and no spaces
# issue, so the plain portable shebang is used as-is.
if command -v cygpath >/dev/null 2>&1; then
  BASH_PATH="$(cygpath -m -s "$(command -v bash)")"
  SHEBANG="#!${BASH_PATH}"
else
  BASH_PATH="bash"
  SHEBANG="#!/usr/bin/env bash"
fi

cat > "$HOOK_FILE" <<EOF
$SHEBANG
# Pre-push: sweep recent Antigravity / Gemini prompts, then submit AI logs.
# Uses the cross-platform Python launcher so it works whether the user
# has python3, python, or only the \`py\` launcher (Windows).
BASH="$BASH_PATH"
"\$BASH" scripts/_pyrun.sh scripts/log_antigravity.py --auto || true
"\$BASH" scripts/_pyrun.sh scripts/submit_log.py || true
exit 0  # Never block push, even if either step fails
EOF

chmod +x "$HOOK_FILE"
chmod +x scripts/_pyrun.sh 2>/dev/null || true
echo "[ai-log] Git pre-push hook installed."

# .gitattributes references a merge driver named "ours" for the ai-log
# scripts, so merging in a teammate's branch doesn't stop for a conflict on
# files this mechanism owns — it just keeps this branch's version. The
# driver itself can't live in .gitattributes (git won't let a repo-tracked
# file define arbitrary executable behavior); each clone sets it locally.
git config merge.ours.driver true
echo "[ai-log] Local 'ours' merge driver configured (see .gitattributes)."

mkdir -p .ai-log
touch .ai-log/.gitkeep

echo "[ai-log] Setup complete. Configure AI_LOG_SERVER in your .env file."
