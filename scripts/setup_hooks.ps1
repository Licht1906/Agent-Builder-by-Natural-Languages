# Install git pre-push hook for AI log submission (Windows PowerShell).
# Run once after cloning: powershell -ExecutionPolicy Bypass -File scripts\setup_hooks.ps1

$ErrorActionPreference = 'Stop'

$HookFile = '.git/hooks/pre-push'

# Git on Windows runs hooks via Git Bash, so the hook body must be bash.
# The shebang must be an absolute, space-free path — not a bare `bash` or
# `#!/usr/bin/env bash`. Windows System32 ships a WSL launcher also named
# bash.exe, and whatever spawns `git push` (VS Code's Source Control
# button, a GUI client, a stale-PATH shell...) doesn't reliably resolve a
# bare name to Git's own bash ahead of that stub; when it wins, the hook
# fails outright with "Windows Subsystem for Linux has no installed
# distributions" and blocks every push through that client. A shebang
# with a *space* in the path ("C:/Program Files/Git/...") isn't parsed
# correctly either, so this resolves Git's own bash to its 8.3 short path
# (no spaces, no PATH search) once, here at setup time, and bakes it into
# the generated hook.
$BashCmd = Get-Command bash.exe -ErrorAction SilentlyContinue
if ($BashCmd) {
    $Fso = New-Object -ComObject Scripting.FileSystemObject
    $BashPath = ($Fso.GetFile($BashCmd.Source).ShortPath) -replace '\\', '/'
    $Shebang = "#!$BashPath"
} else {
    $BashPath = 'bash'
    $Shebang = '#!/usr/bin/env bash'
}

$HookBody = @"
$Shebang
# Pre-push: sweep recent Antigravity / Gemini prompts, then submit AI logs.
BASH="$BashPath"
"`$BASH" scripts/_pyrun.sh scripts/log_antigravity.py --auto || true
"`$BASH" scripts/_pyrun.sh scripts/submit_log.py || true
exit 0
"@

Set-Content -Path $HookFile -Value $HookBody -Encoding UTF8 -NoNewline
Write-Host "[ai-log] Git pre-push hook installed."

# .gitattributes references a merge driver named "ours" for the ai-log
# scripts, so merging in a teammate's branch doesn't stop for a conflict on
# files this mechanism owns — it just keeps this branch's version. The
# driver itself can't live in .gitattributes; each clone sets it locally.
git config merge.ours.driver true
Write-Host "[ai-log] Local 'ours' merge driver configured (see .gitattributes)."

if (-not (Test-Path .ai-log)) { New-Item -ItemType Directory -Path .ai-log | Out-Null }
if (-not (Test-Path .ai-log/.gitkeep)) { New-Item -ItemType File -Path .ai-log/.gitkeep | Out-Null }

Write-Host "[ai-log] Setup complete. Configure AI_LOG_SERVER in your .env file."
