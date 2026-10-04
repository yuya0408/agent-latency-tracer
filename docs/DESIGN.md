# 設計書: agent-latency-tracer

最終更新: 2026-10-04

「今どこまでできていて、次に何をやるか」を把握するための設計書。目的・構成・事例集は
[README.md](../README.md)、事例の出典は [latency-pitfalls-survey.md](latency-pitfalls-survey.md)。

## 1. 決定事項

| 項目 | 決定 | 理由 |
|---|---|---|
| MCPサーバの置き場所 | AgentCore Runtime | AWSのAIエージェント向けサービスの経験を得るため。比較の対象ではなく、土台として使う |
| 認証 | IAMの署名(SigV4) | 手元のクレデンシャルで済み、Cognito(ユーザー管理)が要らない |
| RAG | RDS for PostgreSQL + pgvector、VPCの中だけ | 検索はMCPサーバのツールの1つで、使うかどうかはLLMが決める。AgentCoreのVPCモードでRDSにつなぎ、RDSはインターネットに出さない |
| MCPのSDKとプロトコルの版 | 公式Python SDK `mcp==2.3.0`(低レベルAPI)。プロトコルはSDKの初期設定のまま(2026-07-28版。相手が古い版なら自動で切り替わる) | 新しく作るなら何も指定しない状態が自然。古い版に固定する理由(「今は古い版が多い」)は推測にすぎなかった |
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
 │   └ MCPクライアント          │                  │        ├ 業務ツール(モック)              │
 │ 事例ごとに1か所だけ          │                  │        └ 文書検索 ─VPC─▶ RDS(pgvector)  │
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
| [tracing/instrumentation.py](../tracing/instrumentation.py) | OTel GenAI規約・MCP規約のspan builder、グローバルProviderの登録。保存は`batch=True`で`BatchSpanProcessor`(テストは`SimpleSpanProcessor`) | ✅(TTFTを追加予定) |
| [tracing/exporter.py](../tracing/exporter.py) | spanをSQLiteに保存(手元用) | ✅ |
| [tracing/viewer/waterfall.py](../tracing/viewer/waterfall.py) | 単一HTMLのwaterfall(手元用) | ✅ |
| [system/tools/](../system/tools/) | 業務ツールの定義＋モック実行(200〜800ms) | ✅(定義は仮。§6の2bで作り直す) |
| [system/retrieval/](../system/retrieval/) | 手元用の検索(faiss)、合成ベクトルの生成 | ✅(合成ベクトルはpgvectorへの投入にも使う) |
| [system/mcp_server/](../system/mcp_server/) | 自作MCPサーバ(低レベルAPI、Streamable HTTP、`127.0.0.1:8000/mcp`)。業務ツール＋文書検索`retrieve_documents`。計装オンのときはHTTPリクエストごとのspan(`opentelemetry-instrumentation-asgi`)も出す。`MCP_SERVER_OTEL=0`で計装オフ | ✅ |
| [system/mcp_client.py](../system/mcp_client.py) | SDKの`Client`を包み、tool呼び出しを`execute_tool` spanで囲む。HTTPヘッダにも`traceparent`を載せる(`httpx2`の`event_hooks`。SigV4の署名もここに足す見込み)。`async with`の間は接続を使い回す | ✅ |
| [scripts/try_mcp.py](../scripts/try_mcp.py) | 手元の動作確認。ツール3つを「1つずつ」と「同時に」で呼び、waterfallを`out/`に出す | ✅ |
| `system/llm/` | OpenAI互換クライアント | ⬜ |
| `system/agent.py` | エージェントループ | ⬜ |
| `infra/` | Terraform(VPC・RDS・AgentCore・CloudWatchの設定) | ⬜ |
| `docs/runbook.md`・`traces/` | 手順書と根拠のトレース | ⬜ |

## 4. span設計

### 今あるもの

| span | 作るもの | 主な属性 |
|---|---|---|
| `invoke_agent` (root) | 自作 | `gen_ai.agent.name` |
| `retrieval` | 自作 | `gen_ai.retrieval.top_k` |
| `execute_tool <ツール名>` | 自作(`mcp_client.py`) | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `mcp.method.name`, `mcp.protocol.version` |
| `chat` | 自作 | `gen_ai.usage.input_tokens` / `output_tokens` |
| `MCP send <メソッド>`(CLIENT) | SDK(手元側) | `mcp.method.name`, `jsonrpc.request.id`。この中で`params._meta`に`traceparent`を書き込む |
| `POST /mcp`(SERVER) | `opentelemetry-instrumentation-asgi`(サーバ側) | HTTPリクエストを受け取ってから応答を返し終わるまで。親はHTTPヘッダの`traceparent` |
| `<メソッド>`(SERVER、例: `tools/call search_master`) | SDK(サーバ側のmiddleware) | `mcp.method.name`, `mcp.protocol.version`, `gen_ai.tool.name`, `error.type` |

- SDKのspanはグローバルのTracerProviderに送られる。登録しないと捨てられ、`traceparent`も送られない
  (`set_global_tracer_provider`)
- HTTP越しでは`execute_tool` → `MCP send tools/call` → サーバの`tools/call`の3段が1本のトレースになる。
  HTTPを使わない接続(テストで使う)では`MCP send`が作られず、サーバのspanが`execute_tool`の直下につく
- 2026-07-28版のMCPにはあいさつ(`initialize`)もセッションIDも無い。最初の`server/discover`は版の問い合わせだけ

### 足すもの

- **MCPサーバ側**: 検索ツールの中のDBクエリのspan
- **AgentCoreの基盤側**(AWSが出す): `InvokeAgentRuntime` span(`latency_ms`、`session.id`)、
  メトリクス(`Latency`、`SessionCount`等)。AgentCoreが付ける`Mcp-Session-Id`は`mcp.session.id`に記録する
- **LLM**: TTFT(最初のトークンの時刻)

### 見分けにくいと予想している組み合わせ(再現して確かめる)

| 組み合わせ | 予想される見え方 | 見分けるのに要りそうなもの |
|---|---|---|
| #1 逐次await / #2 ブロッキング呼び出し | どちらもtool spanが階段状 | 並列化を意図したかの記録、イベントループの遅延 |
| R リトライ | 1つのtool spanの中に隠れる | 試行ごとのspan event |
| M2 クライアントの作り直し | tool呼び出しが一律に遅い | `server/discover`/`tools/list`のspan、AgentCoreの`Mcp-Session-Id`と新しいmicroVMかどうか |
| #8 長い入力 / #9 長い出力 | どちらもchat spanが長い | TTFT |
| ネットワーク / AWSの基盤 / サーバのコード | エージェント側のspanだけでは区別できない | AgentCoreの`InvokeAgentRuntime` span、サーバ側span、新しいセッションかどうか |

## 5. 確認が必要なこと(作る段階で確かめる)

- AgentCoreの`InvokeAgentRuntime` spanが、自分のspanと1本のトレースにつながるか
- VPCモードのとき、コンテナからCloudWatchにトレースを送る経路(VPCエンドポイントやNATが要ると固定費がかかる)
- AgentCore Runtimeの課金(待機中にも費用がかかるか)
- 手元からCloudWatchにOTLPで送るときの受付口の仕様と署名の方法
- RDSで使えるpgvectorのバージョン
- この PC の CPU で llama.cpp の小さいモデルがどれくらいの速さで動くか(ツール定義入りのプロンプトで)
- **AgentCoreのセッションID**: AgentCoreはMCPの版に関係なく`Mcp-Session-Id`を付け、それで同じmicroVMに振り分ける。
  付け直さないとリクエストごとに新しいmicroVMに行き、コールドスタートで遅くなりうる(公式の
  MCP protocol contract)。2026-07-28版で話すとき、SDKのクライアントがこのIDを送り返すかは未確認
- **AgentCoreに載せるときのサーバ設定**: AgentCoreは`stateless_http=True`を推奨。今はSDKの初期設定(False)なので、
  載せるときに切り替える。ホストは`0.0.0.0`、ARM64のコンテナ
- **ツールの結果に中身を持たせるか**: 今のモックは`{"status": "ok"}`しか返さない。§6の3でLLMを入れて、空の結果のせいで
  LLMの動きが不自然になるなら、ツール名と引数から合成データを返す関数を足す

## 6. 次にやること

- [x] 1. AWS CLIとTerraformの確認(2026-10-03: WSLのUbuntuに導入済み。AWS CLI 2.36、Terraform 1.15、東京リージョンのIAMユーザーで認証できる)
- [x] 2. 自作MCPサーバ(低レベルAPI)と計装つきMCPクライアントを、手元で動かす(2026-10-04。§7の「動かしていて見つかったこと」)
- [x] 2a. §7の2(正体の分からない待ち時間)を調べる(2026-10-04。原因は自作のSQLite保存。§7の2)
- [ ] 2b. 業務ツールのカタログを作り直す。今の40個は`toolcall-serving-bench`のカタログ(選択を難しくするため、
  似た機能のツールをわざと並べたもの)の流用で、このプロジェクトとしての根拠が無い。方針は、Anthropicの
  [ツール設計の指針](https://www.anthropic.com/engineering/writing-tools-for-agents)(APIをそのまま包まず用途ごとにまとめる、
  機能の重なりを避ける)に沿った形を基本にし、公開されている業務システムのMCPサーバ(freee・Salesforce・
  Dynamics 365など。少数の汎用ツール型が多い)も調べてから決める。「APIをそのまま包んだ汎用ツール(往復が増える)」
  「似たツールを大量に並べる(M1、選び間違い)」は事例の落とし穴として再現する候補
  - 2026-10-05 調査([tool-catalog-survey.md](tool-catalog-survey.md))を踏まえて決めたこと:
    - **汎用型にする。** 操作ごとにツールを分け(`describe_data`・`find_records`・`get_record`等)、データの種類は引数で
      指定する。公式の業務MCPサーバの多く(freee・Salesforce・Dynamics 365・NetSuite)がこの形。ツール1つに操作まで
      まとめる形は、引数の形を操作ごとに書けず、span名から操作が読めなくなるので採らない
    - **業務の区分ではツールを分けない。** 区分(販売・購買・在庫・会計・人事、＋共通マスタ)は、データの種類に付ける
      ラベルにする。M1の修正(ツールを絞る)で使える。旧カタログの「レポート」区分は文書検索と役割が重なるので外す
    - 落とし穴版(データの種類ごとの自動生成)と、ツールの絞り方は、事例を作る段階(§6の6)で決める
    - 未決: データの種類、集計の扱い、書き込みの操作を入れるか
- [ ] 3. llama.cppの速度を測り、エージェントループを手元で組む
- [ ] 4. Terraform: VPC・RDS(pgvector)・AgentCore・CloudWatch。データ投入と、pgvectorの検索の計測
- [ ] 5. MCPサーバをAgentCoreに預け、トレースをCloudWatchに集める
- [ ] 6. 事例を1つずつ再現し、手順書に書き、トレースを書き出す

## 7. 動かしていて見つかったこと(手順書の「自然に発生」の候補)

2と3はこのPCのディスクと自作のSQLite保存に固有の部分があるが、「同期的な保存・ログ送信がリクエストの処理を止める」
形は実務でも起きる。

1. **`tools/call`のたびに、サーバが内部で`tools/list`ハンドラを実行する。** 2026-07-28版のStreamable HTTPで、
   `Mcp-Param-*`ヘッダの検証にツールの引数の形が要るため(SDKの`Server.get_tool_input_schema`のdocstring)。
   この処理はMCPの層が`traceparent`を読む前に走るので、MCPの層の計装だけでは**親の無い別のトレースに記録され**、
   エージェントのトレースからは「`MCP send`とサーバの`tools/call`の間の隙間」としてしか見えなかった。HTTPの層の
   span(`POST /mcp`)を足し、HTTPヘッダにも`traceparent`を載せると、その子としてトレースの中に見えるようになった。
   今のサーバでは一覧がメモリ上にあり約1ms。一覧の組み立てが重いサーバでは呼び出しのたびに効く。
   `Server(..., get_tool_input_schema=...)`を渡すと無くなる。SDKの初期設定のまま残している
2. **計装の保存がツールの応答を遅らせていた(原因判明、2a)。** HTTP越しで、`MCP send tools/call`の時間からサーバの
   `tools/call`の時間を引いた差が12〜100ms(80ms前後が多い)あった。切り分けの順:
   - HTTPの層のspan → 待ち時間は通信ではなくサーバの中、それも「ツール処理が終わってから応答を送り始めるまで」
   - asyncioのデバッグモード(`loop.slow_callback_duration`を20msに) → その区間でイベントループが30〜70ms
     同期的に止まっている。サーバの計装をオフにすると消える
   - 保存1回ごとの計測 → `tools/call` spanの保存だけがときどき約67ms。SQLite単体で、**前の書き込みから100ms以上
     空くとcommitが65〜75msかかる**(連続なら4ms)ことを再現。ツール処理の数百msの後に保存されるspanが該当した
   - `SimpleSpanProcessor`はspanの終了時に呼び出し元のスレッドで同期的に保存するので、その間イベントループが止まる
     (#2と同じ形。`span.end()`も`export()`も同期の関数で、`await`を入れる余地が無い)
   - `BatchSpanProcessor`(キューに入れ、別スレッドがまとめて保存)に切り替えると、差は3〜11msになった
   - 確認: 保存先を手元とサーバで別ファイルに分けても変わらなかった(同時書き込みの待ちではない)。spanを作る
     こと自体は1個あたり約0.03msで、問題は保存のほう
3. **計装は無料ではない。** 保存の方式しだいで、測っている対象そのものを遅くする。`BatchSpanProcessor`は止めない代わりに、
   保存まで最大`BATCH_EXPORT_INTERVAL_MS`(500ms)遅れ、終了時に`shutdown()`しないとキューに残ったspanが失われる

## 8. 実測済みの前提値

- PCメモリ総量 13.74GB。大きな配列を作る前に空きを確認する
- 手元のfaissでの検索の実測値はREADMEの「検索についての実測メモ」
- venv: `.venv/Scripts/python.exe`(システムPythonにはfaissが無い)
- AWS: 既存アカウント(東京リージョン)、月60ドルの予算アラートあり。**AWS CLIとTerraformはWSLのUbuntuから使う**
  (Windows側には入っていない。クレデンシャルもUbuntu側の`~/.aws`)
