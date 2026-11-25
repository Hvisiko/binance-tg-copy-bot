# 幣安交易員即時追蹤 ＆ 限價跟單 Telegram 機器人（雲端免本地部署完整版）

這是一個專為**雲端 24 小時託管（完全免開自己電腦、免裝本地環境）**設計的輕量 Python 專案：
1. **第一階段（預設開啟，免填幣安 API Key）**：即時監控幣安合約帶單員（Copy Trading ＆ 舊版排行榜）的**🟢 新開倉、🔼 加倉、🔽 減倉、⚪ 平倉離場**，在 **Telegram** 即時推播交易卡片，並支援**按鈕選單、一鍵追蹤熱門交易員、直接貼網址加入監控**。
2. **第二階段（手機一鍵切換）**：內建**限價單（Limit Order）自動跟單與風控引擎**，支援：
   * **限價開倉控滑點（`TARGET_PRICE` 或 `POST_ONLY_BBO`）**
   * **超時未成交自動撤單（且支援容器重啟後自動恢復掛單追蹤）**
   * **帶單員平倉時自動秒撤掛單 ＋ 保命平倉**
   * **大幣種最低名目價值（`MIN_NOTIONAL`）最大保證金安全防呆**
   * **原生支援幣安 Ed25519 非對稱金鑰**（免綁固定 IP，更內建 Telegram 私訊 `/genkey` 一鍵產生公私鑰，零本地工具也能輕鬆設定！）

---

## 🚀 5 分鐘「免本地部署」上雲端教學（推薦 Zeabur 日本東京節點）

> 💡 **為什麼選 Zeabur（日本東京節點）？**  
> 幣安會封鎖美國雲端機房 IP（報 `HTTP 451`），而 Zeabur 可直接選擇 **Tokyo, Japan（日本東京）** 機房，從你的私人 GitHub 自動打包部署，完全不用在自己電腦安裝 Python 或管理 Linux 主機。

### 步驟 1：在 Telegram 取得機器人 Token（1 分鐘）
1. 在 Telegram 搜尋官方的 **`@BotFather`**，傳送 `/newbot`，依指示取名後複製取得的 **`TELEGRAM_BOT_TOKEN`**（格式如 `123456789:AAHxxx...`）。
2. 點進你剛建好的機器人聊天室，先按一下 **Start（開始）**。

### 步驟 2：上傳至你的私人 GitHub 並部署到 Zeabur（3 分鐘）
1. 登入 GitHub 建立一個 **Private（私人）** Repository，點選 **uploading an existing file**，將本專案資料夾內的所有檔案（含 `src/`、`scripts/`、`tests/`、`Dockerfile`、`requirements.txt`、`zbpack.json`）拖曳上傳並 Commit。
2. 登入 [Zeabur](https://zeabur.com/) 建立新 Project，**地區務必選擇 `Tokyo, Japan`（日本東京）**。
3. 點擊 **Add Service → Git** → 選擇剛才上傳的私人 GitHub 專案。
4. 點進該服務的 **Variables（環境變數）** 分頁，只需先新增 **1 個變數**即可啟動：
   ```env
   TELEGRAM_BOT_TOKEN=你的_Bot_Token
   ```
   *(選填：建議在 Zeabur 服務的 **Storage / Volumes** 掛載 `/app/data` 目錄，讓重新部署時也能永久保留追蹤名單與設定！)*
5. 服務啟動後，打開手機 Telegram 對你的機器人傳送 **`/start`**：
   * 機器人會**自動將你的帳號綁定並永久儲存為唯一管理員**！
   * 點擊面板上的 **「🏆 熱門帶單排行」**，直接按 **「➕ 一鍵追蹤」** 按鈕，或**直接把任何幣安帶單網址貼進對話框**，馬上開始接收即時開平倉推播！

---

## 📱 Telegram 內建互動指令一覽

| 指令 / 按鈕 | 功能說明 |
| :--- | :--- |
| `/start` | 開啟互動按鈕控制面板與系統總覽 |
| `/top` | 抓取幣安近 30 日高收益公開帶單員，附帶 **「➕ 一鍵追蹤」** 按鈕 |
| 直接貼網址 或 `/add <網址/ID>` | 新增追蹤交易員並立即建立基準持倉快照（不會誤觸舊單） |
| `/positions` | 立即查看所有追蹤中交易員的當前持倉、開倉均價與浮動盈虧 |
| `/my` | 查看你自己的幣安合約帳戶 USDT 餘額、當前持倉與待成交限價單（附一鍵全撤按鈕） |
| `/set` | 在手機上即時檢視或修改 `margin`（保證金）、`leverage`（槓桿）、`slippage`（滑點%）、`timeout`（超時撤單秒數）、`mode`、`exit` |
| `/genkey` | **（僅限私訊）** 免電腦工具，直接在 Telegram 一鍵產生幣安免綁 IP 專用的 **Ed25519 公私鑰** |
| `/autotrading on` / `off` | 隨時切換「純即時通知」或「自動限價跟單」 |

---

## ⚙️ 進階：開啟「限價單自動跟單」設定指南（免固定 IP）

當你在 Telegram 觀察幾天通知、確認帶單員表現符合預期後，想開啟**自動限價跟單**：

1. 在 Telegram 一對一私訊你的機器人輸入 **`/genkey`**（或在電腦執行 `python scripts/generate_ed25519_key.py`）。
2. 複製機器人給你的 **公鑰（Public Key）**，進入幣安官網 **帳戶 → API 管理 → 建立 API → 自生成 API 金鑰（Self-generated）** 貼上公鑰，勾選 **「啟用合約（Enable Futures）」**（切勿勾選提現！）。
3. 回到 Zeabur 的 **Variables（環境變數）** 補上：
   ```env
   BINANCE_API_KEY=你的幣安API_Key
   BINANCE_ED25519_PRIVATE_KEY=貼上/genkey產生的單行私鑰字串
   ```
   *(貼好後記得把 Telegram 對話中的 `/genkey` 私鑰訊息長按刪除以策安全！)*
4. 等待服務自動重啟後，在 Telegram 點擊 **「💼 我的帳戶／掛單」** 確認能讀到合約餘額，再點擊 **「🟢 開啟自動限價跟單」** 即大功告成！
