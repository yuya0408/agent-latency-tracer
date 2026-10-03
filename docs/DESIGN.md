# 設計書: agent-latency-tracer

最終更新: 2026-10-03

「今どこまでできていて、次に何をやるか」を把握するための設計書。目的・スコープ・事例集は
[README.md](../README.md)、事例の出典は [latency-pitfalls-survey.md](latency-pitfalls-survey.md)。

## 1. 全体の流れ

```
 ┌──────────────────────── system/ (観測対象) ────────────────────────┐
 │  agent.py (エージェントループ)                                      │
 │    ├─▶ retrieval/ (RAG: faiss)                                     │
 │    ├─▶ llm/ (偽LLM / OpenAI互換 → llama.cpp)                       │
 │    └─▶ MCPクライアント ──HTTP(_meta.traceparent)──▶ mcp_server/    │
 │                                          (サーバ側spanはオン/オフ) │
 │  事例ごとに1か所だけ落とし穴ありに切り替える(他は健全な状態)       │
 └──────────────────────────────┬─────────────────────────────────────┘
                                │ OTel span (tracing/instrumentation.py)
                                ▼
 ┌──────────────────────── tracing/ ──────────────────────────────────┐
 │  SQLiteSpanExporter (exporter.py) → spans.db → waterfall.py → HTML  │
 └──────────────────────────────┬─────────────────────────────────────┘
                                ▼
       waterfallで見え方を確かめる → docs/runbook.md に書く
       見分けられない → 計装を足す → 足す前と後を比べる
```

## 2. コンポーネントと状態

| 場所 | 役割 | 主なAPI | 状態 |
|---|---|---|---|
| [tracing/instrumentation.py](../tracing/instrumentation.py) | OTel GenAI規約のspan builder | `invoke_agent_span` / `retrieval_span` / `execute_tool_span` / `chat_span` / `set_*` | ✅(MCP規約の属性・TTFTを追加予定) |
| [tracing/exporter.py](../tracing/exporter.py) | spanをSQLite 1テーブルに保存 | `SQLiteSpanExporter` / `list_traces` / `read_trace` | ✅ |
| [tracing/viewer/waterfall.py](../tracing/viewer/waterfall.py) | 単一HTMLのwaterfall | `render_trace_html` / `render_index_html` | ✅ |
| [system/tools/](../system/tools/) | ツール定義40個＋モック実行(200〜800ms) | `load_catalog` / `call_tool` | ✅(MCPサーバの中身として使う) |
| [system/retrieval/](../system/retrieval/) | RAG(クラスタ構造の合成ベクトル、Flat/HNSW) | `generate_corpus` / `generate_queries` / `build_flat_index` / `build_ann_index` / `search` / `recall_at_k` | ✅ |
| `system/mcp_server/` | 自作MCPサーバ(低レベルAPI、Streamable HTTP) | — | ⬜ |
| `system/llm/` | 偽LLM / OpenAI互換クライアント | — | ⬜ |
| `system/agent.py` | エージェントループ | — | ⬜ |
| `docs/runbook.md` | 切り分けの手順書 | — | ⬜ |

## 3. span設計

### 今あるもの

| span (`gen_ai.operation.name`) | 主な属性 |
|---|---|
| `invoke_agent` (root) | `gen_ai.agent.name` |
| `retrieval` | `gen_ai.retrieval.top_k` |
| `execute_tool` | `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| `chat` | `gen_ai.usage.input_tokens` / `output_tokens` |

クエリ文・tool引数/結果など中身は`capture_content=True`の時だけ記録する。

### MCPで足すもの(OTel MCP規約)

- クライアント側: `initialize`・`tools/list`のspan、tool呼び出しは既存の`execute_tool` spanに
  `mcp.method.name`・`mcp.session.id`等を足す(外側でtool実行のspanを張っているなら、MCP側で
  別spanを作らない、という規約に従う)
- `params._meta`に`traceparent`を載せてサーバ側spanを同じトレースにつなぐ
- サーバ側: 受けたメッセージごとのspan(オン/オフ)

### 見分けにくいと予想している組み合わせ(再現して確かめる)

| 組み合わせ | 予想される見え方 | 見分けるのに要りそうなもの |
|---|---|---|
| #1 逐次await / #2 ブロッキング呼び出し | どちらもtool spanが階段状 | 並列化を意図したかの記録、イベントループの遅延 |
| #5 全件走査 / #6 1件ずつ呼び出し | どちらも検索が長い | 呼び出し回数・1回あたりの時間 |
| R リトライ | 1つのtool spanの中に隠れる | 試行ごとのspan event |
| M2 セッション張り直し | tool呼び出しが一律に遅い | `initialize`/`tools/list`のspan、`mcp.session.id` |
| #8 長い入力 / #9 長い出力 | どちらもchat spanが長い | TTFT |
| サーバ内 / 通信 / クライアント | サーバ側spanが無いと区別できない | サーバ側span |

## 4. 未決事項

- **#5と#6の組み方**: 健全な状態は「HNSW・サブクエリをまとめて1回」。#5は全件走査に変える、#6は
  1件ずつ呼ぶ。#6の遅さは計算ではなく通信の往復なので、検索を「1回ごとに通信遅延がかかるサービス」
  として呼ぶ形にする。全件走査をまとめて検索した場合(約230ms)で遅さが十分見えるかは実装後に確かめる
- **偽LLMの台本**: LLMのターンを固定の遅延と固定の返答(tool呼び出しの指示を含む)で表す。#3を再現できる形にする
- **ローカルLLM**: llama.cppで動かすモデル(1〜2B程度)。tool呼び出しの判断をモデルに任せるか、台本で決めて
  生成だけさせるか

## 5. 次にやること

- [ ] 1. 自作MCPサーバ(低レベルAPI、Streamable HTTP)と、計装つきMCPクライアント
- [ ] 2. 偽LLMと、健全な状態のエージェントループ(検索・LLM・toolを1本につなぐ)
- [ ] 3. 偽LLMで再現できる事例を1つずつ実装し、waterfallで確かめて手順書に書く
- [ ] 4. llama.cppを用意して、ローカルLLMが必要な事例

## 6. 実測済みの前提値

- PCメモリ総量 13.74GB。大きな配列を作る前に空きを確認する
- `IndexFlatL2`のレイテンシはコーパスサイズにほぼ比例(約8MBで1ms)
- 20クエリを1件ずつ検索 = 749ms、まとめて1回 = 229ms(n=500,000/dim=128、全件走査)
- venv: `.venv/Scripts/python.exe`(システムPythonにはfaissが無い)
