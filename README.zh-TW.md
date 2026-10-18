# exchange-mcp-server（繁體中文）

讓 Claude（或任何 MCP 用戶端）透過 **EWS** 操作 Microsoft Exchange 信箱的 MCP 伺服器：讀信與搜尋、歸檔與清理、
讀寫行事曆、查同仁忙閒、找會議室並預約、寄信（附件、Outlook 簽名檔、延遲傳遞）。

> [!IMPORTANT]
> **Exchange Online（Microsoft 365）**：Microsoft 正在停用 Exchange Online 的 EWS。2026-10-01 起，未設定應用程式白名單
> 的租戶開始分階段封鎖，**2027-04-01 永久下線**。下方 OAuth 模式只適用仍允許 EWS 的租戶。
> **地端 Exchange Server 不受影響**，也是本專案的主要對象。Exchange Online 之後規劃改用 Microsoft Graph。

## 安裝

```bash
pip install "exchange-mcp-server @ git+https://github.com/Rirychh/exchange-mcp-server"
```

## 設定

把 [`.env.example`](.env.example) 複製到以下任一位置（依序找到第一個為準）並填好：

1. 環境變數 `EXCHANGE_MCP_ENV_FILE` 指定的路徑
2. 目前目錄的 `.env`
3. `~/.config/exchange-mcp/.env`
4. `~/exchange_mcp.env`（舊版位置，仍可讀）

地端 Exchange 最少需要：

```ini
EXCHANGE_SERVER=https://mail.example.com/EWS/Exchange.asmx
EXCHANGE_EMAIL=you@example.com
EXCHANGE_PASSWORD=your_password
EXCHANGE_DOMAIN=EXAMPLE
EXCHANGE_TIMEZONE=Asia/Taipei
```

設定完成後執行 `exchange-mcp-server check` 測試連線。完整變數表見 [README.md](README.md#settings)。

## 安全設計

- 會寄出、刪除或修改的工具一律有 `dry_run`，預設 `true` 只回傳預覽，確認後再以 `false` 執行。
- 郵件、行程、會議邀請的刪除都是移到「刪除的郵件」，可還原。
- 本機檔案只能從 `EXCHANGE_ATTACHMENT_DIRS` 列出的資料夾附加；未設定時附件功能關閉。
- `EXCHANGE_READ_ONLY=true` 只開放讀取工具；`EXCHANGE_ALLOW_SEND=false` 移除寄信、回覆與回應邀請。

## 會議室對照表

EWS 不提供會議室容納人數；有些公司把會議室建成一般「人員」帳號，不會出現在會議室清單。
在 `rooms.json` 描述即可（範例見 [rooms.example.json](rooms.example.json)）：

- `invite_as: "resource"`（預設）以資源方式預約；`"attendee"` 以必要與會者邀請該帳號。
- 沒寫 `capacity` 的會議室不會被 `min_capacity` 篩掉，會列在 `capacity_unknown`。
- 改檔後自動重新讀取，不需重啟。

## 子行事曆與全天事件

`exchange_create_event` 支援 `calendar`（子行事曆名稱）與 `all_day`；不填與會者與會議室時只建立約會，不寄邀請。
