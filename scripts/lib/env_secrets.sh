#!/usr/bin/env bash
# UC1-33 (#145) / UC1-34 (#146) — the "generate a real secret into .env,
# never touch an operator's own value" logic `install.sh` (single-node) and
# `import_airgap_artifact.sh` (air-gapped) both need. One copy: two install
# paths silently drifting on how a secret is judged "still the checked-in
# default" is exactly the kind of divergence that turns into a security
# report later, not a place to accept a second, slightly different copy.
#
# Source this file (`source scripts/lib/env_secrets.sh`), set `ENV_FILE`
# first, then call `_ensure_secret KEY dev_default_value`.

_random_hex64() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import secrets; print(secrets.token_hex(32))"
    else
        echo "cần 'openssl' hoặc 'python3' để sinh secret — không tìm thấy cái nào" >&2
        return 1
    fi
}

#: Set `key=value` in `$ENV_FILE` — replacing the line if `key` is currently
#: exactly `dev_default` (the checked-in, known-insecure placeholder),
#: appending it if `key` is absent from the file at all, and leaving it
#: alone otherwise (an operator's own customised value is never touched).
_ensure_secret() {
    local key="$1" dev_default="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        local current
        current="$(grep "^${key}=" "$ENV_FILE" | head -n1 | cut -d= -f2-)"
        if [[ "$current" == "$dev_default" || -z "$current" ]]; then
            local fresh
            fresh="$(_random_hex64)"
            # BSD sed (macOS) requires an argument after -i; GNU sed treats
            # the empty string as "no backup file" the same way — `-i.bak`
            # with an immediate cleanup is the one spelling that works on
            # both without a runtime branch.
            sed -i.bak "s|^${key}=.*|${key}=${fresh}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
            echo "Đã sinh ${key} mới (thay cho giá trị mặc định không an toàn)"
        fi
    else
        local fresh
        fresh="$(_random_hex64)"
        printf '%s=%s\n' "$key" "$fresh" >>"$ENV_FILE"
        echo "Đã thêm ${key} (sinh mới)"
    fi
}
