# 設計書: agent-latency-tracer

最終更新: 2026-10-03

「今どこまでできていて、次に何をやるか」を把握するための設計書。目的・構成・事例集は
[README.md](../README.md)、事例の出典は [latency-pitfalls-survey.md](latency-pitfalls-survey.md)。

## 1. 決定事項

| 項目 | 決定 | 理由 |
|---|---|---|
| MCPサーバの置き場所 | AgentCore Runtime | AWSのAIエージェント向けサービスの経験を得るため。比較の対象ではなく、土台として使う |
| 認証 | IAMの署名(SigV4) | 手元のクレデンシャルで済み、Cognito(ユーザー管理)が要らない |
| RAG | RDS for PostgreSQL + pgvector、VPCの中だけ | 検索はMCPサーバのツールの1つ。AgentCoreのVPCモードでRDSにつなぎ、RDSはインターネットに出さない |
| LLM | 手元のllama.cpp(小さいモデル) | GPU費用を避ける。小さいモデルの思わぬ動きも観測の対象にする。偽LLMは自動テストの中だけで使う |
| トレースの集め先 | CloudWatch(Transaction Search) | AgentCoreの基盤側のspanと同じ場所に集める。実務で標準的なやり方 |
| 手元での開発 | AWS無しでも動く形を残す(検索はfaiss、トレースは自作SQLite＋waterfall) | 開発中の確認と、AWSを使わない読み手のため |
| 事例の書き方 | 事例ごとに1ファイル。落とし穴ありと修正済みを並べる | 落とし穴のコードそのものが読めるようにする |
| 公開する根拠 | トレースをJSONで書き出し、waterfall画像と一緒に`traces/`に置く。アカウントID・ARNは伏せる | 読み手がAWS無しで結果を確かめられるようにする |
| インフラ | Terraform。使うときに作り、終わったら消す。状態ファイルは既存のS3バケットに別キーで保存 | 費用を抑える。状態ファイルにはパスワードが入るためリポジトリに置かない |

## 2. 全体の流れ

```
 手元のPC                                       AWS
 ┌──────────────────────────┐                  ┌──────────────────────────────────────┐
 │ agent.py(エージェントループ) │──MCP+SigV4──────▶│ AgentCore Runtime                     │
 │   ├ llm/ → llama.cpp       │                  │   └ mcp_server/(コンテナ)              │
 │   └ MCPクライアント          │                  │        ├ 業務ツール40個(モック)          │
 │ 事例ごとに1か所だけ          │                  │        └ 検索ツール ─VPC─▶ RDS(pgvector) │
 │ 落とし穴ありに切り替える      │                  │                                      │
 └─────────────┬────────────┘                  │ CloudWatch                           │
               │ OTLP(SigV4)                    │   ├ AgentCoreの基盤span・メトリクス(自動)│
               └───────────────────────────────▶│   ├ サーバ側span(コンテナからADOTで送る)│
                                                │   └ エージェント側span                 │
                                                └──────────────────────────────────────┘
       CloudWatchで見え方を確かめる → docs/runbook.md に書く、トレースをtraces/に書き出す
       見分けられない → 計装を足す → 足す前と後を比べる
```

## 3. コンポーネントと状態

| 場所 | 役割 | 状態 |
|---|---|---|
| [tracing/instrumentation.py](../tracing/instrumentation.py) | OTel GenAI規約のspan builder | ✅(MCP規約の属性・TTFTを追加予定) |
| [tracing/exporter.py](../tracing/exporter.py) | spanをSQLiteに保存(手元用) | ✅ |
| [tracing/viewer/waterfall.py](../tracing/viewer/waterfall.py) | 単一HTMLのwaterfall(手元用) | ✅ |
| [system/tools/](../system/tools/) | ツール定義40個＋モック実行(200〜800ms) | ✅(MCPサーバの中身として使う) |
| [system/retrieval/](../system/retrieval/) | 手元用の検索(faiss)、合成ベクトルの生成 | ✅(合成ベクトルはpgvectorへの投入にも使う) |
| `system/mcp_server/` | 自作MCPサーバ(低レベルAPI、Streamable HTTP、`:8000/mcp`) | ⬜ |
| `system/llm/` | OpenAI互換クライアント | ⬜ |
| `system/agent.py` | エージェントループ | ⬜ |
| `infra/` | Terraform(VPC・RDS・AgentCore・CloudWatchの設定) | ⬜ |
| `docs/runbook.md`・`traces/` | 手順書と根拠のトレース | ⬜ |

## 4. span設計

### 今あるもの

| span (`gen_ai.operation.name`) | 主な属性 |
|---|---|
| `invoke_agent` (root) | `gen_ai.agent.name` |
| `retrieval` | `gen_ai.retrieval.top_k` |
| `execute_tool` | `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| `chat` | `gen_ai.usage.input_tokens` / `output_tokens` |

### 足すもの

- **MCPクライアント側**(OTel MCP規約): `initialize`・`tools/list`のspan。tool呼び出しは既存の`execute_tool`
  spanに`mcp.method.name`・`mcp.session.id`等を足す。`params._meta`に`traceparent`を載せる
- **MCPサーバ側**: 受けたメッセージごとのspan(SDKに組み込みの仕組み。オン/オフ切り替え)。検索ツールの中の
  DBクエリのspan
- **AgentCoreの基盤側**(AWSが出す): `InvokeAgentRuntime` span(`latency_ms`、`session.id`)、
  メトリクス(`Latency`、`SessionCount`等)
- **LLM**: TTFT(最初のトークンの時刻)

### 見分けにくいと予想している組み合わせ(再現して確かめる)

| 組み合わせ | 予想される見え方 | 見分けるのに要りそうなもの |
|---|---|---|
| #1 逐次await / #2 ブロッキング呼び出し | どちらもtool spanが階段状 | 並列化を意図したかの記録、イベントループの遅延 |
| R リトライ | 1つのtool spanの中に隠れる | 試行ごとのspan event |
| M2 セッション張り直し | tool呼び出しが一律に遅い | `initialize`/`tools/list`のspan、`mcp.session.id` |
| #8 長い入力 / #9 長い出力 | どちらもchat spanが長い | TTFT |
| ネットワーク / AWSの基盤 / サーバのコード | エージェント側のspanだけでは区別できない | AgentCoreの`InvokeAgentRuntime` span、サーバ側span、新しいセッションかどうか |

## 5. 確認が必要なこと(作る段階で確かめる)

- AgentCoreの`InvokeAgentRuntime` spanが、自分のspanと1本のトレースにつながるか
- VPCモードのとき、コンテナからCloudWatchにトレースを送る経路(VPCエンドポイントやNATが要ると固定費がかかる)
- AgentCore Runtimeの課金(待機中にも費用がかかるか)
- 手元からCloudWatchにOTLPで送るときの受付口の仕様と署名の方法
- RDSで使えるpgvectorのバージョン
- この PC の CPU で llama.cpp の小さいモデルがどれくらいの速さで動くか(ツール定義40個入りのプロンプトで)

## 6. 次にやること

- [ ] 1. AWS CLIとTerraformの導入、クレデンシャルの確認(読み取りのみ)
- [ ] 2. 自作MCPサーバ(低レベルAPI)と計装つきMCPクライアントを、手元で動かす(検索はfaiss、トレースはSQLite)
- [ ] 3. llama.cppの速度を測り、エージェントループを手元で組む
- [ ] 4. Terraform: VPC・RDS(pgvector)・AgentCore・CloudWatch。データ投入と、pgvectorの検索の計測
- [ ] 5. MCPサーバをAgentCoreに預け、トレースをCloudWatchに集める
- [ ] 6. 事例を1つずつ再現し、手順書に書き、トレースを書き出す

## 7. 実測済みの前提値

- PCメモリ総量 13.74GB。大きな配列を作る前に空きを確認する
- 手元のfaissでの検索の実測値はREADMEの「検索についての実測メモ」
- venv: `.venv/Scripts/python.exe`(システムPythonにはfaissが無い)
- AWS: 既存アカウント(東京リージョン)、月60ドルの予算アラートあり。このPCには`~/.aws`の設定がある
