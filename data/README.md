# 自備資料

本庫不含真實語料、向量、索引或其衍生資料；僅含明示為假的測試產生器。使用者須自行取得語料並確認著作權、使用與外送處理授權。MIT 僅涵蓋本庫程式與文件，不涵蓋任何語料或第三方內容。

## 建置輸入

`RAG_SOURCE` 指向自備來源根目錄（預設為專案下 `source/`）；`RAG_CONTEXT_DIR` 為其內的 context 子目錄（預設 `context`）。不要將來源提交到 Git。

- `manifest/videos.frozen.json`：`count` 與 `videos`，每部含 `video_id`（11 字元）、`title`、`duration_s`、`url`。
- `context/videos/<video_id>/segments.json`：含 `video_id`、`chunk_id`、`source_segment_index`、`reader_text`、`reader_original_text`、`start`、`end`、`timing_status`、`raw_path`、`raw_sha256` 的列清單。
- `context/pending.json`、`corrections.json`、`normalization.json`（含 `policy`）、`source-sha256.json`：待判卡、校訂與來源 hash 對照。
- `raw/<video_id>/chunks.json`：含 `chunks`，每個切片有 `chunk_id`、`idx`、`start`；其他 raw JSON 須符合列內 hash。
- `transcripts/<video_id>/openai__whisper-1/segments.jsonl`：含 `chunk_id`、`source_segment_index`、`text_raw`；該目錄名是匯入 schema，不限定必須使用此供應商。
- 可選 `quality-flags.json`：`video_id` 與 `items`（`chunk_id`、`flags`，列級另帶 `source_segment_index`）。

`tests/synthetic.py` 會產生完整的 2 部假影片、4 列「測試句」輸入，示範結構而不模仿任何真實語錄。測試使用明示的字元 tokenizer 替身與 1536 維單位向量，均非供應商 tokenizer／embedding，不適用語意品質評測。

`build.py` 輸出 `chunks.jsonl`、`videos.json`、`sources.json`、`build-manifest.json` 及 `local.sqlite`。向量索引另需 `vectors.npy`／`vector-ids.json` 及 `embedding-manifest.json`；這些不隨 Git 提供。

## SHA-256 manifest

過去交付包的檔名／SHA-256 清單已隨原資料說明保存在庫外私人驗收資料夾，未改動；本庫不再公布真實語料的指紋。操作者可用 `sha256sum` 或 `shasum -a 256` 建立自己的核對清單，於庫外保存。此處沒有真實資料 manifest。
