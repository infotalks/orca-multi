#!/usr/bin/env bash
# 安装 orca-multi skill 到 Claude Code 和/或 Codex，可装到本机或远程主机。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/skills/orca-multi"
TARGET="all"
HOST=""

usage() {
  cat <<'USAGE'
用法：
  ./install.sh [--target claude|codex|all] [--host SSH主机]

  --target   安装到哪个客户端（默认 all）
               claude → ~/.claude/skills/orca-multi
               codex  → ~/.codex/skills/orca-multi（SKILL.md 中的路径改为 ~/.codex）
  --host     通过 ssh 安装到远程主机（例如 min）；不填则装到本机

已存在的同名目录会被整体替换。
USAGE
}

fail() { echo "ERROR: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --target) (($# >= 2)) || fail "--target 需要参数"; TARGET="$2"; shift 2 ;;
    --host) (($# >= 2)) || fail "--host 需要参数"; HOST="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

case "$TARGET" in
  claude) CLIENTS=(claude) ;;
  codex) CLIENTS=(codex) ;;
  all) CLIENTS=(claude codex) ;;
  *) fail "--target 只能是 claude、codex 或 all" ;;
esac
[[ -z "$HOST" || "$HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "--host 含不支持的字符"
[[ -f "$SRC/SKILL.md" ]] || fail "找不到 skill 源码：$SRC"
command -v rsync >/dev/null || fail "需要 rsync"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/orca-multi-install.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT

for client in "${CLIENTS[@]}"; do
  out="$STAGE/$client"
  rsync -a --exclude='__pycache__' --exclude='.DS_Store' --exclude='._*' "$SRC/" "$out/"
  if [[ "$client" == codex ]]; then
    sed -i.bak 's#~/\.claude/skills/orca-multi#~/.codex/skills/orca-multi#g' "$out/SKILL.md"
    rm -f "$out/SKILL.md.bak"
  fi
  chmod +x "$out/scripts/orca-multi"

  dest=".$client/skills/orca-multi"
  if [[ -n "$HOST" ]]; then
    ssh -o BatchMode=yes "$HOST" "mkdir -p '.$client/skills'"
    COPYFILE_DISABLE=1 rsync -a --delete "$out/" "$HOST:$dest/"
    echo "已安装：$HOST:~/$dest"
  else
    mkdir -p "$HOME/.$client/skills"
    rsync -a --delete "$out/" "$HOME/$dest/"
    echo "已安装：~/$dest"
  fi
done

echo
echo "验证（在目标机器上运行）："
for client in "${CLIENTS[@]}"; do
  echo "  python3 -m unittest discover -s ~/.$client/skills/orca-multi/scripts"
done
