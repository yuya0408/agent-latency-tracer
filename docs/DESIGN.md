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
| LLM | 手元のllama.cpp(公式Dockerイメージ`ghcr.io/ggml-org/llama.cpp:server`、b11434)で、Qwen3.5-2B(Q4_K_M、1.28GB)。考える過程の出力(thinking)はオフ | GPU費用を避ける。小さいモデルの思わぬ動きも観測の対象にする。2Bは、1回の応答で独立したツール呼び出しを複数出し(#1の前提)、結果に中身があれば止まって答えることを確かめた(§8)。偽LLMは自動テストの中だけで使う |
| トレースの集め先 | CloudWatch(Transaction Search) | AgentCoreの基盤側のspanと同じ場所に集める。実務で標準的なやり方 |
| 手元での開発 | AWS無しでも動く形を残す(検索はfaiss、トレースは自作SQLite＋waterfall) | 開発中の確認と、AWSを使わない読み手のため |
| 事例の書き方 | 事例ごとに1ファイル。落とし穴ありと修正済みを並べる | 落とし穴のコードそのものが読めるようにする |
| 公開する根拠 | トレースをJSONで書き出し、waterfall画像と一緒に`traces/`に置く。アカウントID・ARNは伏せる | 読み手がAWS無しで結果を確かめられるようにする |
| 手元の実行環境 | WSLのUbuntu。エージェント・テスト・開発中のMCPサーバはWSLのvenv、llama.cppは公式のDockerイメージ。venvと、測定中に書き込むトレースのSQLiteはWSLのホーム側に置く。リポジトリはWindows側のまま、WSLから`/mnt/c`越しに実行する | このPCではスマートアプリコントロールが署名の無いDLL(faiss)を止める。通信がWindowsとWSLをまたがないようそろえる。本番(AgentCore)もLinuxのコンテナ。`/mnt/c`越しのファイルの読み書きは遅いが、コードを読むのは起動時だけなので測定には混ざらない |
| インフラ | Terraform。使うときに作り、終わったら消す。状態ファイルは既存のS3バケットに別キーで保存 | 費用を抑える。状態ファイルにはパスワードが入るためリポジトリに置かない |
| エージェントの基本形 | 1回の応答で頼まれたツール呼び出しは`asyncio.gather`で同時に実行し、MCPの接続とツールの一覧は使い回す。システムプロンプトは付けない。LLMを呼ぶのは1回の質問につき最大10回 | 事例の「直した書き方」の基準。10はOpenAI Agents SDKの`DEFAULT_MAX_TURNS`(LangChainは15、smolagentsは20)。日付をシステムプロンプトに入れる話は#10の事例で扱う |
| LLMへのリクエスト | ストリーミングで受け、temperatureは0にする。`max_tokens`は付けない | ストリーミング: TTFTはllama.cpp以外のサーバでも測れ、チャットの体感の速さを決め、llama.cppの読み込み時間との差で通信などの分が分かる。temperature: llama.cppの初期値0.8では、同じ質問でツール呼び出しの数が2つになったり4つになったりした。`max_tokens`: 主なフレームワークは初期値で付けない。止まらない生成(§7の9)は#9の材料なので、基準の側で隠さない |
| LLMのモデルの大きさ | 2Bのまま進める。大きさは比べない | 事例の遅さはこちらで作り込むもので、LLMに要るのは「ツールを呼ぶ・複数頼む・止まる」だけ。文書検索を選ばない件(§7の8)は、事例の質問の書き方で扱う |

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
| [tracing/instrumentation.py](../tracing/instrumentation.py) | OTel GenAI規約・MCP規約のspan builder、グローバルProviderの登録。保存は`batch=True`で`BatchSpanProcessor`(テストは`SimpleSpanProcessor`) | ✅ |
| [tracing/exporter.py](../tracing/exporter.py) | spanをSQLiteに保存(手元用) | ✅ |
| [tracing/viewer/waterfall.py](../tracing/viewer/waterfall.py) | 単一HTMLのwaterfall(手元用) | ✅ |
| [system/tools/](../system/tools/) | 業務ツール4つの定義(`catalog.json`)、データの種類と項目(`data_schema.json`)、合成データ(`mock_data.json`)、モック実行(200〜800ms待ってから、合成データを条件で絞り・集計して返す) | ✅ |
| [system/retrieval/](../system/retrieval/) | 手元用の検索(faiss)、合成ベクトルの生成 | ✅(合成ベクトルはpgvectorへの投入にも使う) |
| [system/mcp_server/](../system/mcp_server/) | 自作MCPサーバ(低レベルAPI、Streamable HTTP、`127.0.0.1:8000/mcp`)。業務ツール＋文書検索`retrieve_documents`。計装オンのときはHTTPリクエストごとのspan(`opentelemetry-instrumentation-asgi`)も出す。`MCP_SERVER_OTEL=0`で計装オフ | ✅ |
| [system/mcp_client.py](../system/mcp_client.py) | SDKの`Client`を包み、tool呼び出しを`execute_tool` spanで囲む。HTTPヘッダにも`traceparent`を載せる(`httpx2`の`event_hooks`。SigV4の署名もここに足す見込み)。`async with`の間は接続を使い回す | ✅ |
| [scripts/try_mcp.py](../scripts/try_mcp.py) | 手元の動作確認。ツール3つを「1つずつ」と「同時に」で呼び、waterfallを`out/`に出す | ✅ |
| [scripts/run_agent.py](../scripts/run_agent.py) | 手元の動作確認。仮の質問(§6の3)をエージェントに流し、質問ごとのwaterfallを`out/`に出す | ✅ |
| [system/llm/](../system/llm/) | OpenAI互換クライアント。ストリーミングで受け、chat spanにTTFTとllama.cppの内訳(`timings`)を記録する。断片で届くツール呼び出しを組み立てる | ✅ |
| [system/agent.py](../system/agent.py) | エージェントループ(§1の「エージェントの基本形」) | ✅ |
| `infra/` | Terraform(VPC・RDS・AgentCore・CloudWatchの設定) | ⬜ |
| `docs/runbook.md`・`traces/` | 手順書と根拠のトレース | ⬜ |

## 4. span設計

### 今あるもの

| span | 作るもの | 主な属性 |
|---|---|---|
| `invoke_agent` (root) | 自作(`agent.py`) | `gen_ai.agent.name`。上限で打ち切ると`error.type=max_turns_exceeded` |
| `retrieval` | 自作 | `gen_ai.retrieval.top_k` |
| `execute_tool <ツール名>` | 自作(`mcp_client.py`) | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `mcp.method.name`, `mcp.protocol.version` |
| `chat <モデル名>` | 自作(`llm/client.py`) | `gen_ai.usage.input_tokens` / `output_tokens`、`gen_ai.request.temperature`、`gen_ai.response.finish_reasons`、`llm.time_to_first_token_ms`(クライアント側のTTFT)、`llamacpp.prompt_n` / `cache_n` / `prompt_ms` / `predicted_n` / `predicted_ms`(llama.cppの内訳) |
| `MCP send <メソッド>`(CLIENT) | SDK(手元側) | `mcp.method.name`, `jsonrpc.request.id`。この中で`params._meta`に`traceparent`を書き込む |
| `POST /mcp`(SERVER) | `opentelemetry-instrumentation-asgi`(サーバ側) | HTTPリクエストを受け取ってから応答を返し終わるまで。親はHTTPヘッダの`traceparent` |
| `<メソッド>`(SERVER、例: `tools/call find_records`) | SDK(サーバ側のmiddleware) | `mcp.method.name`, `mcp.protocol.version`, `gen_ai.tool.name`, `error.type` |

- SDKのspanはグローバルのTracerProviderに送られる。登録しないと捨てられ、`traceparent`も送られない
  (`set_global_tracer_provider`)
- HTTP越しでは`execute_tool` → `MCP send tools/call` → サーバの`tools/call`の3段が1本のトレースになる。
  HTTPを使わない接続(テストで使う)では`MCP send`が作られず、サーバのspanが`execute_tool`の直下につく
- 2026-07-28版のMCPにはあいさつ(`initialize`)もセッションIDも無い。最初の`server/discover`は版の問い合わせだけ

### 足すもの

- **MCPサーバ側**: 検索ツールの中のDBクエリのspan
- **AgentCoreの基盤側**(AWSが出す): `InvokeAgentRuntime` span(`latency_ms`、`session.id`)、
  メトリクス(`Latency`、`SessionCount`等)。AgentCoreが付ける`Mcp-Session-Id`は`mcp.session.id`に記録する

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
- **ツールの結果に中身を持たせるか**(2026-10-08に解決): `{"status": "ok"}`だけを返すモックでは、LLMが作り話をしたり
  同じ呼び出しを繰り返したりした。0件と明示する・条件を無視した仮の行を返す・小さな合成データを条件で絞る、の3通りを
  試し、2段以上の質問が止まるのは最後だけだった。そこで、15種類それぞれ数件の合成データを持たせ、`filter`で絞って返す
  形にした(DBの遅さを見るためではなく、会話を自然に進めるため。待ち時間は今までどおりモックの200〜800ms)

## 6. 次にやること

- [x] 1. AWS CLIとTerraformの確認(2026-10-03: WSLのUbuntuに導入済み。AWS CLI 2.36、Terraform 1.15、東京リージョンのIAMユーザーで認証できる)
- [x] 2. 自作MCPサーバ(低レベルAPI)と計装つきMCPクライアントを、手元で動かす(2026-10-04。§7の「動かしていて見つかったこと」)
- [x] 2a. §7の2(正体の分からない待ち時間)を調べる(2026-10-04。原因は自作のSQLite保存。§7の2)
- [x] 2b. 業務ツールのカタログを作り直す(2026-10-06)。それまでの40個は`toolcall-serving-bench`のカタログ(選択を難しくするため、
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
    - **データの種類は15個。** 受注から入金まで(受注・出荷・請求)と発注から支払まで(発注・入荷・仕入請求)の2つの流れに、
      在庫(残高・入出庫)・会計(仕訳・勘定科目)・人事(従業員・勤怠)の中心と、共通マスタ(取引先・品目・部門)を足した。
      受注などには取引先IDと並べて取引先名も持たせ、名前で直接絞れるようにした(取引先マスタを先に引く往復は入れない)
    - **集計は`aggregate_records`を1つ足す。** 全件を引いてLLMに計算させると結果が大きく(M4)、小さいLLMは計算も誤りやすい
    - **書き込みの操作は入れない。** 事例集の遅延は読み取りで再現できる
    - **`find_records`の`limit`は既定100・上限2000。** 既定はodata_mcp_go、上限はSalesforceの値。外した版はM4の落とし穴の候補
    - 条件(`filter`)は`{field, op, value}`の並び(ANDだけ)。実例に多いクエリ文字列より、引数の形として検証しやすい
- [x] 3. llama.cppの速度を測り、エージェントループを手元で組む。最初にWSLでの環境作り(venv、テストの通過確認、WSL2のメモリ上限の確認)
  - [x] WSLのvenv、llama.cppの起動、速度の実測(§8)、ツール呼び出しの確認、モックへの合成データ(2026-10-08)
  - [x] OpenAI互換のクライアント(`system/llm/`)とエージェントループ(`system/agent.py`)(2026-10-11)。
    仮の質問6つが実機で最後まで動き、同じ応答の2つのツール呼び出しが同時に動くことをwaterfallで確かめた
  - 動作確認用の仮の質問(事例ごとの質問は§6の6で決める。評価用に固定するものではない):
    1. A社の未出荷の受注は? 2. A社の未出荷の受注と、品目X001の在庫は? 3. 2026年9月の、取引先ごとの受注金額の合計は?
    4. B社からの仕入請求で、支払期日が10月中のものは? 5. 品目X001の入荷待ちの発注は? 6. 出張旅費の精算のルールは?
- [ ] 4. Terraform: VPC・RDS(pgvector)・AgentCore・CloudWatch。データ投入と、pgvectorの検索の計測
  - [x] 土台: 状態ファイルの置き場所(既存のバケット、別キー。バケット名は`infra/backend.hcl`に分けてコミットしない)、
    VPC・プライベートサブネット2つ(AZ IDで`apne1-az1`・`az2`)・S3のgatewayエンドポイント。作って消せることを確かめた(2026-10-12)
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
4. **ツールの結果の形が、学習で見たものと違うとLLMが迷う。** 中身の無い`{"status": "ok"}`では、2Bが存在しない受注を答えたり、
   同じ集計を繰り返して打ち切りになったりした。「0件」と分かる形で返すと止まる。往復の回数がそのまま遅延になる
5. **小さいLLMの項目の選び間違いが、往復を増やす。** `describe_data`で項目を調べた後でも、取引先IDの項目に名前を入れる、
   品目コードを`code`で探す、在庫を品目マスタで探す、といった誤りがあった。知らない項目名にはエラーで使える項目名を返す。
   エージェントループでも、取引先IDの項目に「A」「A社」を入れて0件になった(2026-10-11)
6. **最初の1問だけ、LLMの読み込みが極端に遅い。** コンテナを4日間放置した後の最初のリクエストで、21トークンの読み込みに
   10.1秒かかった(次の質問からは同じ量で約0.7秒)。モデルのデータがメモリから追い出されていたためと推測しているが、
   確かめていない。chat spanの`llamacpp.prompt_ms`と`prompt_n`の比で見つかる
7. **質問のたびに、まず`describe_data`を呼ぶ。** 項目を調べてから検索する手順を毎回踏み、LLMとの往復が1回増える。
   その結果(約250トークン)を次の呼び出しで読み込むのに4〜8秒かかった。汎用型のツールの往復の多さ(§6の2b)が
   自然に出た形
8. **文書検索は、ツールの説明文の言葉が質問に無いと選ばれない。** 文書にしか答えの無い質問8つのうち、
   「社内規程では」「手順書を見て」と書いた2つでだけ`retrieve_documents`を呼び、残りは業務データのツールを探った
   (2B、2026-10-11)。RAGを使う事例では、質問の書き方でこれを避ける
9. **生成が止まらず、文脈の上限まで書き続けた。** 2回、それぞれ約9分かかり、1回は6,719トークンを生成して
   上限(8192)で打ち切られた(llama.cppのログで`truncated = 1`)。`max_tokens`を付けていないので止める仕組みが無い。
   #9(長い出力)の極端な形。chat spanの`gen_ai.usage.output_tokens`と`finish_reasons`で見つかる
10. **TTFTとllama.cppの読み込み時間に差がある。** ツールを呼んだ1回目の呼び出しでは約600ms、答えを返す呼び出しでは
    50〜100ms。ツール呼び出しの書き出し部分の生成がTTFTに混ざると推測しているが、ツールを呼んで差が155msの回もあり、
    確かめていない

## 8. 実測済みの前提値

- PCメモリ総量 13.74GB。大きな配列を作る前に空きを確認する
- 手元のfaissでの検索の実測値はREADMEの「検索についての実測メモ」
- venv: WSLの`~/.venvs/agent-latency-tracer`(Python 3.14.4、faiss 1.15.1。2026-10-06作成、全テスト通過)。
  Windows側の`.venv/Scripts/python.exe`(3.13)はfaissが読み込めないため、faissを使わない作業にだけ使える
- 2026-10-06時点で、このPCではWindowsのスマートアプリコントロールがfaissのDLLの読み込みを止める。faissを読み込む
  テスト(`test_retrieval.py`・`test_mcp.py`)とMCPサーバは、Windows側では動かない(10-04までは動いていた)。対処は手元の
  実行環境をWSLに移すこと(§1)。2aのSQLiteの値・READMEのfaissの実測値はWindows側での値
- WSL2が使えるメモリは初期設定で約6GB(PCの半分)
- **llama.cpp(Qwen3.5-2B、CPU 4スレッド、2026-10-07)**:
  - 5ツールの定義は約1360トークン(質問だけなら21)
  - prefill(プレフィックスキャッシュをオフにして測定)は毎秒約55トークンで、長さにほぼ比例: 1381トークンで24秒、2732で48秒、
    6782で127秒。decodeは毎秒約12.7トークン
  - キャッシュが効くと(初期設定)、2回目以降は新しく足された分だけを読む。1つの質問はLLM 3回前後で、合計30〜90秒
  - エージェントで仮の質問6つ(2026-10-11): 1問18〜63秒、LLMを2〜3回。時間のほとんどはLLMで、ツールの実行は1回1秒未満。
    長かったのは、ツールの結果を読み込む2回目の呼び出し(11〜26秒)と、長い答えの生成(652トークンで54秒)
- WSLでの`try_mcp.py`(2026-10-06): `MCP send tools/call`とサーバの`tools/call`の差は6〜7ms(Windowsでの2aの対処後は3〜11ms)
- AWS: 既存アカウント(東京リージョン)、月60ドルの予算アラートあり。**AWS CLIとTerraformはWSLのUbuntuから使う**
  (Windows側には入っていない。クレデンシャルもUbuntu側の`~/.aws`)
- Terraformの土台(VPC等7つ): applyもdestroyも約1分。消した直後は、札で探すAPI(Resource Groups Tagging API)に
  消したものがしばらく残る。消し忘れの確認は、IDを指定して直接問い合わせる
- このアカウントのAZの名前とID: `1a=apne1-az4`、`1c=apne1-az1`、`1d=apne1-az2`
