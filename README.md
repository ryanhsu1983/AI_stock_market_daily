# AI_stock_0050

台股監控與提醒程式。程式會抓取市場資料，依照加權模型產生 HTML Email 報告，目前主要追蹤台灣加權指數、台積電、聯發科。

正式工作目錄：

C:\Users\zergv\Documents\GitHub\AI_stock_0050

GitHub repo：

https://github.com/ryanhsu1983/AI_stock_0050

目前主要分支：

codex/restore-working-version

## 主要檔案

- 0050_signal_system.py：主程式，負責抓資料、計算訊號、產生 HTML 報告與寄信。
- config.json：追蹤標的、指標門檻、Email 與事件設定。
- .github/workflows/daily_run.yml：GitHub Actions 每日自動執行設定。
- preview_email.bat：本機雙擊產生 Email 預覽用。
- email_preview.html：本機執行後產生的預覽檔，不應提交到 Git。

## 目前模型

使用加權分數模型，分別計算買進分數與賣出分數。主要指標包含趨勢、MACD、三大法人、KD、OBV、匯率、利率、量能。

BIAS60 用來判斷中期過熱或超跌，不直接加分。當 BIAS60 顯示過熱時，買進分數會被鎖定為 0，避免追高。

## 報告呈現原則

報告上方有「評分標準」可展開區塊。

今日總覽與各股詳細指標都整理為投資者容易理解的格式：

- 市場狀態
- 操作方向
- 說明原因

例如：

大多頭 / 過熱鎖定 / 禁止追買，核心部位續抱觀察

## 交易邏輯方向

目前模型偏向風險控管與分批進出提醒，不是單純追求大多頭報酬最大化。

- 大多頭：少賣，弱賣出通常只提醒，不急著下車。
- 多頭修正：保留核心部位，中強訊號才考慮減碼。
- 盤整：較適合依訊號分批操作。
- 空頭：賣出訊號權重提高，買進訊號保守看待。

同一等級訊號連續出現時，不建議每天重複交易。

## 顏色規則

多數地方採用台股閱讀習慣：

- 紅色：上漲、買超、偏多
- 綠色：下跌、賣超、偏空

例外：趨勢環境目前特別設定為：

- 多頭健康：綠色
- 空頭確認：紅色

## 本機測試

在正式工作目錄執行：

python 0050_signal_system.py

執行後會產生 email_preview.html。

如果本機沒有設定 GMAIL_PASSWORD，程式會跳過寄信，但仍會產生 HTML 預覽。

## GitHub Actions

GitHub Actions 設定為每天台灣時間早上 8 點執行一次，產生報告並寄信。

寄信需要在 GitHub repo 的 Secrets 設定：

GMAIL_PASSWORD

## 給新 Codex 聊天的接手提示

如果開新聊天，請先確認：

git status
python -m py_compile 0050_signal_system.py

之後所有修改、測試、commit、push 都應該在正式工作目錄進行：

C:\Users\zergv\Documents\GitHub\AI_stock_0050

舊的 Codex 日期資料夾不再作為主要工作區。
