@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ⚠️ 刻意保留主控台視窗，不要改回 pythonw + start。
rem
rem 實測被 Cortex XDR 中斷。以下組合正是 EDR 眼中的惡意行為特徵：
rem   - pythonw（無視窗）
rem   - start（脫離父行程）
rem   - 背景攔截全系統鍵盤（RIDEV_INPUTSINK）
rem   - 注入按鍵（SendInput）
rem   - 讀取剪貼簿
rem 保留可見的主控台，至少拿掉「隱藏行程 + 脫離父行程」這兩項。
rem 而且畫面上的訊息就是唯一的即時狀態來源。
rem
rem 若真的不想要視窗，請改用捷徑（.lnk）指向 pythonw.exe，
rem 那是正常的應用程式啟動方式，比 .cmd + start 自然得多。

python app\vibetalkie.py %*
set RC=%errorlevel%

if not "%RC%"=="0" (
  echo.
  echo [結束碼 %RC%] 程式非正常結束。
  echo   若被防毒中斷：把這個資料夾加入排除清單（需要管理員權限，
  echo   公司電腦可能要請 IT 處理）。
  pause
)
