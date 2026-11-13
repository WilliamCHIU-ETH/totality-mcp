# Transcript Evidence MCP

唯讀 RAG MCP 服務：搜尋候選片段、讀取證據、列出來源與健康檢查。它不產生答案，不把檢索排名當成語意信心。關鍵字模式預設不呼叫外部 API。

本庫不含真實語料或其衍生資料；測試只用明示的人造假資料。使用者須自備語料並自行確認著作權、授權及外送處理權限。**MIT 僅涵蓋本庫程式與文件，不涵蓋任何語料或第三方內容。**

維護前請先閱讀 [AGENTS.md](AGENTS.md)，並在專案根目錄執行 `python3 tools/probe.py`，以即時探測結果確認分支、CI、部署與服務現況。

## 架構與安全預設

- MCP 與 PostgreSQL + pgvector 是分開的服務。DB 不公開埠，MCP 使用僅有 SELECT 的 reader 角色；loader 僅能透過受限函式建立／封存候選及 CAS 切版，不取得 owner DSN、DDL 或刪除權限。
- SQLite + NumPy 為離線工程測試 backend。stdio 繼承本地啟動者權限，不提供多人隔離。
- HTTP 必須提供至少 32 bytes 的 Bearer token，預設 host 綁 `127.0.0.1`。Compose 容器內的監聽不同於 host publication：host 只發佈 `127.0.0.1:8765`。
- Host 必須符合 allowlist；如有 Origin 也須符合 allowlist。不因公開程式碼而開放服務埠或取消認證。
- 持有 token 者可讀所有授權給服務的全文片段；本版本沒有每人／每庫 ACL 或每身分 rate limit。來源文字是不受信任資料，不能當指令。
- Token、DSN、API key、資料與備份不入 Git。client 範例使用佔位符；不要把 token 寫入範例設定。

## 自備語料與建置

Python 3.12；輸入 schema 見 `data/README.md`。先在隔離目錄安裝依賴：

```bash
cd /path/to/project
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
RAG_SOURCE=/path/to/corpus RAG_DATA=/path/to/new-data .venv/bin/python build.py
```

建置使用 tiktoken 的 `cl100k_base`，首次 tokenizer 初始化可能下載公開編碼資料；離線使用前須自行備妥快取。切片上限 350 tokens，邊界重疊最多 50 tokens，保留來源、未核時序與待判版本。不要在正在服務的 release 上重建。

向量建置與自動 query embedding 都可能外送文字並產生費用，必須先取得批准。本庫不提供 API key。若要執行，明確設定 `RAG_OPENAI_KEY_FILE=/path/to/key-file`；未設定不得讀取任何預設憑證庫。`embed.py --dry-run` 不發送 API；`--cache-only` 在快取不足時必須停止。自動查詢 adapter 預設關閉，需明確設定 `RAG_ENABLE_QUERY_EMBEDDING=1` 才啟用，並使用獨立可寫 `RAG_QUERY_LEDGER_DIR`。本程式的本地預留帳本不是供應商帳戶 spending cap；費率常數不是現行報價保證。

## 部署（操作者自行核對環境後執行）

在 `<your-host>` 的 `/path/to/project` 準備合法資料與備份；Compose 配方目前為 linux/arm64，其他架構須另核對映像。以下命令只適用全新核准目錄，不要直接對既有部署覆蓋操作：

```bash
cd /path/to/project
python3 deploy/prepare_host.py
docker compose config --quiet
docker compose up -d rag-db
docker compose --profile import run --rm loader
# Empty database only: explicitly approve the first activation.
docker compose --profile import run --rm loader --dsn-file /run/secrets/loader_dsn --activate --allow-initial
docker compose up -d rag-mcp
```

`prepare_host.py` 會自行產生新的隨機部署 token 與 DB 憑證（已有秘密則拒絕覆寫），並收緊檔案權限；不要提交產物。Compose 專案名為 `transcript-mcp`；遷移既有服務時須核對 volume／network 名稱，不能直接當作原部署的原地升級。跨主機使用經驗證的 SSH tunnel／私網；若要公開服務需另外審查 TLS、代理、ACL、資源與濫用防護。

```bash
sh deploy/backup.sh
sh deploy/restore_verify.sh /path/to/backup.dump
```

先在獨立測試 DB 驗還原；不刪既有資料或使用 `down -v` 當回退。新版 release 匯入後，以 `deploy/activate.py --dsn-file /path/to/loader-dsn --release <release-id> --expected-base-release <current-release-id>` 明確切換。候選 manifest 的 `base_release` 必須等於目前 active；空庫才可用 `--allow-initial`。回退保留版需額外明示 `--rollback`，仍必須提供目前版的 CAS；回退不是刪除或備份。

### 既有庫升級：需要手動步驟

1. 先備份，在隔離庫演練；以具備角色管理權的部署 owner／DBA 執行 `deploy/migrate-release-v2.sql`（可重跑）；角色 bootstrap 與權限收斂需要 DBA，日常 schema DDL 則由 `rag_owner` 擁有。`rag_owner` 為 NOLOGIN 物件擁有者；由 DBA 明確授予 migration 登入身分所需會員資格，不授予 loader/runtime。
2. 以 owner 執行 `deploy/backfill-videos.py --dsn-file /path/to/owner-dsn --release <existing-release-id> --videos-file /path/to/existing-videos.json`，驗證影片集合與片段一致。未回填會 fail closed，不會退用本地舊 metadata。
3. 為既有 reader 登入保留 `rag_reader`；為獨立 ingest 登入授予 `rag_loader`。無 superuser、CREATEROLE、CREATEDB、DDL 或直接表格寫入；另核對登入身分沒有繼承其他高權限角色。新建空庫的角色／密碼設定見 `deploy/init-reader.sh`，不是既有庫密碼輪替工具。
4. 更新 Compose 的 loader secrets：`loader_password`、`loader_dsn`。`owner_dsn` 僅供操作者 migration／備份，不掛入 loader。服務 `requirements.lock` 不含 STT worker 依賴，入庫 worker 使用獨立 venv。
5. 再部署應用程式。Postgres 模式以 DB 的每版 `videos` 與 `manifest` 為準，不依賴啟動時本地 JSON；每個工具呼叫先讀同一個 active 指標，固定該次查詢版本，切版／回退不需重啟 MCP。不同工具呼叫間可能跨版；舊 chunk ID 在非當前版回 `not_found`。

匯入不再自動執行 DDL，也不自動啟用。完整產物需包含 `videos.json`；舊版 metadata 回填必須先於新應用部署。程式部署與資料發布是兩個獨立批准閘門。

## 分支與自動部署

- `dev` 是開發分支，`main` 是正式環境。日常變更推到 `dev`，CI 會跑離線合成測試。
- 要上線時開 `dev` → `main` 的 PR；`main` 受保護，只接受測試通過的 PR。
- 合併到 `main` 後 Deploy workflow 會重跑測試，並等待 repo 擁有者在 `production` 環境核准，核准後才連到主機。
- 主機端的部署金鑰綁定單一強制指令：只接受一個已在 `main` 上的 commit SHA，只重建並重啟 MCP 服務，健康檢查失敗即退回上一版。
- 自動部署只更新應用程式檔（`Dockerfile`、`requirements.lock`、`*.py`）。`compose.yaml`、`schema.sql`、`deploy/`、資料、憑證與資料庫都不會被自動變更，需由操作者審查後手動處理。

## 離線合成測試

```bash
PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_synthetic.py
```

規模為 2 部假影片、4 列「測試句」、最多 4 個 1536 維人造單位向量；不呼叫 API、不啟動 Docker、不開 HTTP listener。測試守衛拒絕 socket 連線、DNS 解析與子程序。建置測試注入字元 tokenizer 替身，沒有驗證真實 tokenizer；向量亦非模型生成。真實語料品質、MCP SDK 握手及 PostgreSQL 整合需在另行配置的環境驗證；私人驗收資料見 `qa/README.md`。

## 版本與狀態

此為服務程式的泛化來源版本，release ID 由自備輸入的雜湊產生，不固定指向作者的私人語料版本。依作者提供的狀態，正式部署已在作者環境驗證；本次清理未連線重新驗證該環境。**本 repo 的第三方依賴授權與公開服務安全性未經完整審查。** `requirements.lock` 的版本與映像 digest 不是授權或無漏洞證明。MIT 授權見 `LICENSE`，不擴張到資料與第三方元件。
