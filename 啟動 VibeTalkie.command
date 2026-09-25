#!/bin/bash
# VibeTalkie 的 macOS 雙擊入口。
#
# 在 Finder 裡雙擊 .command 就會執行。第一次可能要允許執行：
#     chmod +x "啟動 VibeTalkie.command"
#
# ⚠️ 目前 VibeTalkie 只有 Windows 實作，這個入口會啟動 launch.py，
#    由它明確告知「此平台尚未支援」並停住，不會神秘失敗。

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo
    echo "  ✗ 找不到 python3。請先安裝 Python 3.11 以上。"
    echo "    https://www.python.org/downloads/"
    echo
    read -r -p "按 Enter 關閉…"
    exit 1
fi

"$PY" launch.py "$@"
RC=$?

if [ "$RC" -ne 0 ]; then
    echo
    echo "[結束碼 $RC]"
    read -r -p "按 Enter 關閉…"
fi
