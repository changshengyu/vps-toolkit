#!/usr/bin/env bash
# 公共函数：由 vps.sh 和 scripts/ 下的脚本加载

info() { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "请使用 root 运行。"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

# 用法：confirm "提示内容"，输入 y 或 yes 时返回成功
confirm() {
    local answer
    read -r -p "$1 [y/N]: " answer || return 1
    case "$answer" in
        y | Y | yes | YES | Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# 下载到临时文件，通过语法检查后运行，并把附加参数传给脚本
run_remote_script() {
    local url="$1"
    local script rc=0
    shift

    require_command curl
    script="$(mktemp)"

    if ! curl -fsSL "$url" -o "$script"; then
        rm -f "$script"
        die "下载失败：$url"
    fi

    if ! bash -n "$script"; then
        rm -f "$script"
        die "下载的脚本未通过语法检查：$url"
    fi

    bash "$script" "$@" || rc=$?
    rm -f "$script"
    return "$rc"
}
