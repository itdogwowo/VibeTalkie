#!/bin/bash
# VibeTalkie 的 macOS 雙擊入口。
#
# 在 Finder 裡雙擊 .command 就會執行。第一次可能要允許執行：
#     chmod +x "啟動 VibeTalkie.command"
#
# 這個入口會啟動 app/mac_vibetalkie.py（macOS 的實作）。
# 它自己會處理：
#   · Python 版本太舊 → 自動找一個合格的重新執行（透過 launch.py）
#   · 缺相依套件／模型 → 自動下載安裝
#   · 輔助使用／麥克風權限沒開 → 講清楚要去哪裡開
#
# 為什麼不直接跑 launch.py：
#   launch.py 是**跨平台啟動器**，它會先檢查平台，macOS 目前仍會回
#   「尚未支援」（那是給還沒有實作的平台看的）。macOS 已經有實作了，
#   所以要直接進 mac 的進入點。

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

"$PY" app/mac_vibetalkie.py "$@"
RC=$?

if [ "$RC" -ne 0 ]; then
    echo
    echo "[結束碼 $RC]"
    read -r -p "按 Enter 關閉…"
fi
