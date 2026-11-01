# 設計書: agent-latency-tracer

最終更新: 2026-10-31

「今どこまでできていて、次に何をやるか」を把握するための設計書。目的・構成・事例集は
[README.md](../README.md)、事例の出典は [latency-pitfalls-survey.md](latency-pitfalls-survey.md)。

## 1. 決定事項

| 項目 | 決定 | 理由 |
|---|---|---|
| MCPサーバの置き場所 | AgentCore Runtime | AWSのAIエージェント向けサービスの経験を得るため。比較の対象ではなく、土台として使う |
| エージェントの置き場所 | 段階5b以降はAgentCore Runtime(MCPサーバとは別のRuntime)。GPUのEC2とは同居させない。利用者役の手元のスクリプトが、`InvokeAgentRuntime`で呼ぶ | 一般的な形に合わせる。推論サーバ(GPU)とエージェント(CPUのアプリ)は、役割も増やし方も違うので分けて置き、同じVPCの中から呼ぶ。AgentCore RuntimeはAWSでエージェントを動かすための専用サービスで、既に土台として使っている。段階5まではエージェントを手元に置き、MCPサーバの移行と分けて確かめる |
| 認証 | IAMの署名(SigV4) | 手元のクレデンシャルで済み、Cognito(ユーザー管理)が要らない |
| RAG | RDS for PostgreSQL + pgvector、VPCの中だけ | 検索はMCPサーバのツールの1つで、使うかどうかはLLMが決める。AgentCoreのVPCモードでRDSにつなぎ、RDSはインターネットに出さない |
| MCPのSDKとプロトコルの版 | 公式Python SDK `mcp==2.3.0`(低レベルAPI)。プロトコルはSDKの初期設定のまま(2026-07-28版。相手が古い版なら自動で切り替わる) | 新しく作るなら何も指定しない状態が自然。古い版に固定する理由(「今は古い版が多い」)は推測にすぎなかった |
| LLM | 段階5までは手元のllama.cpp(公式Dockerイメージ`ghcr.io/ggml-org/llama.cpp:server`、b11434)で、Qwen3.5-2B(Q4_K_M、1.28GB)。段階5b以降は、GPUのEC2(g6.xlarge、NVIDIA L4)でvLLM 0.31.0とQwen3-8B bf16(2026-10-31決定)。どちらもthinkingはオフ | 段階5までは、つながることを確かめるだけでLLMの性能は効かないので、費用のかからない手元で進める。事例の再現(段階6)では、LLMが時間の大半を占めないGPUにする(「LLMの動かし方とモデルの大きさ」)。偽LLMは自動テストの中だけで使う |
| 段階5b以降のエンジン | vLLM 0.31.0、Qwen3-8B bf16。リクエストごとの内訳は応答の本文から取り(`--enable-per-request-metrics`・`--enable-prompt-tokens-details`)、エンジンのspan(`--otlp-traces-endpoint`)は補助にする | `toolcall-serving-bench`(L4 1台・Qwen3-8B)で精度の足切りを通ったのはvLLM bf16・FP8とTensorRT-LLM bf16・FP8の4構成で、目標の並行数が2以下では同じ台数で並び、ベンチは決めていない。このプロジェクトは質問を1つずつ流すので並行数は1〜2で、ベンチの結論からは決まらない。そこで、事例8/9・10・M1・12がエンジンの中で起きることから、中を見る手段で選んだ。vLLMはHTTPの`traceparent`を親にして`llm_request` spanを作り、待ち行列・プリフィル・デコードの時間を付ける。応答の本文にも待ち行列の時間とキャッシュに当たったトークン数が入り、llama.cppの`timings`と同じ作りで記録できる(v0.31.0のソースで確認)。版と起動の手順はベンチで固定済み。**採らなかった案**: TensorRT-LLM(1.2.1のトレースは試作段階の扱いで、親の引き継ぎを確かめていない。並行数4以上の台数の利点は、並行数1〜2では効かない)。FP8(並行数1〜2では台数の利点が無く、vLLMのFP8はL4ではW8A16で計算する、という注意書きが増える。文脈の長さがbf16で載らなければ見直す)。SGLang・AWQ(ベンチで精度の非劣化を示せなかった)。エンジンを比べ直すことはしない |
| 出口の無いVPCへのvLLMと重みの入れ方 | vLLMはコンテナにしてECRに置き、重みはS3に置いて、ゲートウェイ型エンドポイントから取り、起動のたびにインスタンス付属のNVMeへコピーして読む(§8)。どちらも`infra/`とは別に残す。最初の1回だけ、公開IPつきの仮のGPUサーバでイメージと重みを移し、起動フラグ・文脈の長さ・`traceparent`の引き継ぎを確かめる | コンテナでGPUを動かす経験を得るため(学ぶ目的)。公式イメージ`vllm/vllm-openai:v0.31.0`(圧縮時10.13GB)の保管は月約$1で、費用は決め手にならない。`transformers`はベンチの5.17.0に合わせ、イメージの版が違えばその1行だけ足す。**採らなかった案**: Python環境を固めてS3に置く(保管は月約$0.25で、ベンチの手順をそのまま使えるが、差は小さい)。NAT(作るたびに転送料金がかかり、VPCに外への出口ができる)。重みをAMIやコンテナに入れる(AMIはスナップショットから初めて読む部分が遅く、コンテナは重みを変えるたびに作り直しになる) |
| エンジンのspanの中継 | GPUのEC2でOpenTelemetry Collectorのcontrib版を、版を固定して動かす。vLLMからgRPCで受け、`sigv4auth`で署名してOTLP/HTTPで`xray`の受付口へ送る。spanを加工する部品は入れない | vLLMの送信部品はSigV4で署名できず、受付口はgRPCを受けない。AWSのCollectorの手順に沿え、AWS以外の環境でも同じ形で使える。**採らなかった案**: CloudWatch agent(今のAWSの文書が大抵の場合に勧めており設定も短いが、利点の多くはここで使わない連携にある)。ADOT Collector(役割も設定もほぼ同じで、contrib版で動かなければ切り替える)。以前ADOTを外した理由はPython側のSDKの話で、Collectorの選定には当てはまらない |
| 遅かった1件の見つけ方 | エージェントが質問ごとにtrace_idと所要時間を構造化ログに出す | 画面のトレースの一覧は索引(1%)に入ったものしか出ないので、「遅かった」と言われた1件はログからtrace_idを引いて開く(§5)。手順書の最初の段にする |
| トレースの集め先 | CloudWatch(Transaction Search) | AgentCoreの基盤側のspanと同じ場所に集める。実務で標準的なやり方 |
| Transaction Searchの設定 | `infra/account/`(状態ファイルは別のキー)で有効にし、段階ごとに消す`infra/`とは分けて残す。送り先はCloudWatch Logs、索引は初期値の1% | アカウントとリージョンに1つの設定。Terraformの説明では、送り先と索引のリソースを消してもAWS側の設定は戻らず、X-Rayに書き込みを許すログのリソースポリシーだけが消えて壊れる。spanを送らなければ費用はかからない。索引の割合は段階5で決める(§5) |
| 手元からCloudWatchへの送信 | ADOTを使わず、OTel標準のOTLP/HTTPの送信部品に、botocoreでSigV4の署名をする`requests`のセッションを渡す(ADOTの`OTLPAwsSpanExporter`と同じ作り)。SQLiteへの保存は残し、CloudWatchは2つ目の送り先にする(`run_agent.py --cloudwatch`) | ADOTはOTelのSDKを1つの版に固定し、約50個の計装を一緒に入れ、TracerProviderを自分で組む前提で、このプロジェクトの`build_tracer_provider`と合わない。段階5のコンテナ(AgentCoreの公式の手順はADOTが前提)では見直す。SQLiteを残すのは「手元での開発」と同じ理由 |
| 手元での開発 | AWS無しでも動く形を残す(検索はfaiss、トレースは自作SQLite＋waterfall) | 開発中の確認と、AWSを使わない読み手のため |
| 事例の書き方 | 事例ごとに1ファイル。落とし穴ありと修正済みを並べる | 落とし穴のコードそのものが読めるようにする |
| 公開する根拠 | トレースをJSONで書き出し、waterfall画像と一緒に`traces/`に置く。アカウントID・ARNは伏せる | 読み手がAWS無しで結果を確かめられるようにする |
| 手元の実行環境 | WSLのUbuntu。エージェント・テスト・開発中のMCPサーバはWSLのvenv、llama.cppは公式のDockerイメージ。venvと、測定中に書き込むトレースのSQLiteはWSLのホーム側に置く。リポジトリはWindows側のまま、WSLから`/mnt/c`越しに実行する | このPCではスマートアプリコントロールが署名の無いDLL(faiss)を止める。通信がWindowsとWSLをまたがないようそろえる。本番(AgentCore)もLinuxのコンテナ。`/mnt/c`越しのファイルの読み書きは遅いが、コードを読むのは起動時だけなので測定には混ざらない |
| インフラ | Terraform。使うときに作り、終わったら消す。状態ファイルは既存のS3バケットに別キーで保存 | 費用を抑える。状態ファイルにはアカウントIDやリソースのIDが入るためリポジトリに置かない |
| 手元からRDSへの接続(作業用) | Session Managerのポート転送。踏み台のEC2(t4g.nano、公開IPなし、受け入れの許可なし)と、SSMのインターフェース型エンドポイント3つ(ssm・ssmmessages・ec2messages)を、1つ目のサブネットに置く。RDSと一緒に作って消す | 表を作る・データを入れる・測るための裏口で、本ランの経路(AgentCore→RDS)には入らない。AWSがRDS向けの手順を公式に出している定番。EC2 Instance Connect Endpointは無料だが、公式の手順はEC2向けだけで、RDSへ直接つなぐ例は第三者のものしかなく、使えるポートの記述も食い違っていたので採らなかった。SSHで入る公開の踏み台は、インターネットゲートウェイが要るので採らない |
| RDSの設定 | PostgreSQL 18(小さい版の番号は固定しない)、db.t4g.medium(メモリ4GB)、パスワードはRDSに作らせSecrets Managerに置く(`manage_master_user_password`)、踏み台と同じAZ、バックアップなし | 18はRDSの初期設定。小さい版まで固定すると、その版が選べなくなったときに作れなくなるので、代わりに測るときの実際の版を§8に記録する。mediumは、表とHNSWのインデックスがメモリに収まる大きさ(一般的な選び方。収まらないとディスクの遅さが事例5に混ざる)。microでは表だけで`shared_buffers`に収まらなかった。パスワードが状態ファイルに入らない。AZをまたぐ1ms前後の遅れを検索の時間に混ぜない |
| pgvectorの測定の条件 | 1024次元×10万件(合成ベクトル、seedは0)。HNSWのパラメータはpgvectorの初期値(`m=16`・`ef_construction=64`・`ef_search=40`)。質問100個、top-5。時間は`EXPLAIN (ANALYZE, TIMING OFF)`のDBの中の実行時間で、1周温めてから測る | 条件は結果を見る前に、現実の値から決めた(差が出るように逆算しない)。1024はTitan Text Embeddings V2の初期値・bge-m3・multilingual-e5-largeの次元。10万件は中規模の社内文書(5,000件×20チャンク程度)。top-5はMCPサーバの初期値。手元のfaiss(128次元×50万件)は遅さを見せるための逆算で、手元の確認用に残す |
| AgentCore Runtimeのネットワーク | VPCモード。サブネットは1つ目(`apne1-az1`)だけ。インターフェース型エンドポイント4つ(`ecr.api`・`ecr.dkr`・`logs`・`xray`)を同じサブネットに置き、受け入れはRuntimeのセキュリティグループからだけ。実行ロールは公式の例から、ECRをこのリポジトリに絞り、Bedrockのモデルの権限と`logs:PutResourcePolicy`を外した | ecr・logsは、外への出口が無いVPCでは公式に必須。xrayはトレースをCloudWatchに送る受付口。AWSの推奨はAZの違う2つ以上のサブネットだが、可用性は目的の外で、RDSと別のAZに置かれるとAZをまたぐ遅れが検索に混ざるので1つにした(エンドポイントの費用もAZの数だけ倍になる)。Runtime本体は、中に入れる本物のイメージ(MCPサーバ)と一緒に段階5で作る |
| エージェントの基本形 | 1回の応答で頼まれたツール呼び出しは`asyncio.gather`で同時に実行し、MCPの接続とツールの一覧は使い回す。システムプロンプトは付けない。LLMを呼ぶのは1回の質問につき最大10回 | 事例の「直した書き方」の基準。10はOpenAI Agents SDKの`DEFAULT_MAX_TURNS`(LangChainは15、smolagentsは20)。日付をシステムプロンプトに入れる話は#10の事例で扱う |
| LLMへのリクエスト | ストリーミングで受け、temperatureは0にする。`max_tokens`は付けない | ストリーミング: TTFTはllama.cpp以外のサーバでも測れ、チャットの体感の速さを決め、llama.cppの読み込み時間との差で通信などの分が分かる。temperature: llama.cppの初期値0.8では、同じ質問でツール呼び出しの数が2つになったり4つになったりした。`max_tokens`: 主なフレームワークは初期値で付けない。止まらない生成(§7の9)は#9の材料なので、基準の側で隠さない |
| LLMの動かし方とモデルの大きさ | 事例の再現はGPUの8Bで行う。モデルの大きさは比べない | 手元のCPUで2Bを動かすと、1問18〜63秒のほとんどをLLMが占め、ツールの実行は1回1秒未満だった(§8)。事例の差(数十ミリ秒〜数秒)がLLMの時間に埋もれ、どの遅れが体感に効くかの比率が実際のサービス(GPUで推論)と大きく違う。答えの正しさは目的の外のまま。2026-10-11に「2Bのまま」と決めたが、この点を見落としていたため改めた |

## 2. 全体の流れ

```
 手元のPC                AWS(東京)
 ┌──────────────┐       ┌──────────────────────────────────────────────────────┐
 │ 利用者役の      │       │ VPC                                                  │
 │ スクリプト      │─Invoke─▶│ AgentCore Runtime: agent.py(エージェントループ)        │
 │(質問を投げる)  │ AgentRuntime│   ├ chat ─────────▶ GPUのEC2: 推論サーバ(Qwen3-8B)   │
 └──────────────┘ (SigV4) │   └ MCP+SigV4 ──▶ AgentCore Runtime: mcp_server/       │
                         │                       ├ 業務ツール(モック)               │
                         │                       └ 文書検索 ──▶ RDS(pgvector)      │
                         │ CloudWatch: 基盤span・サーバ側span・エージェント側span  │
                         └──────────────────────────────────────────────────────┘
 段階5までは、エージェントとllama.cpp(2B)を手元に置き、MCPサーバだけをAgentCoreに置く
 事例ごとに1か所だけ落とし穴ありに切り替える → CloudWatchで見え方を確かめる → docs/runbook.mdに書く、トレースをtraces/に書き出す
 見分けられない → 計装を足す → 足す前と後を比べる
```

## 3. コンポーネントと状態

| 場所 | 役割 | 状態 |
|---|---|---|
| [tracing/instrumentation.py](../tracing/instrumentation.py) | OTel GenAI規約・MCP規約のspan builder、グローバルProviderの登録。保存は`batch=True`で`BatchSpanProcessor`(テストは`SimpleSpanProcessor`)。送り先は複数渡せ、それぞれに別のprocessorを付ける | ✅ |
| [tracing/exporter.py](../tracing/exporter.py) | spanをSQLiteに保存(手元用) | ✅ |
| [tracing/aws_exporter.py](../tracing/aws_exporter.py) | spanをCloudWatch(Transaction Search)に送る。OTLP/HTTPとSigV4(依存は`aws`の組) | ✅ |
| [tracing/viewer/waterfall.py](../tracing/viewer/waterfall.py) | 単一HTMLのwaterfall(手元用) | ✅ |
| [system/tools/](../system/tools/) | 業務ツール4つの定義(`catalog.json`)、データの種類と項目(`data_schema.json`)、合成データ(`mock_data.json`)、モック実行(200〜800ms待ってから、合成データを条件で絞り・集計して返す) | ✅ |
| [system/retrieval/](../system/retrieval/) | 手元用の検索(faiss)、合成ベクトルの生成 | ✅(合成ベクトルはpgvectorへの投入にも使う) |
| [system/mcp_server/](../system/mcp_server/) | 自作MCPサーバ(低レベルAPI、Streamable HTTP、`/mcp`、ステートレス)。業務ツール＋文書検索`retrieve_documents`(faissかpgvector。RDSにはIAMのトークンでログイン)。AgentCore用のコンテナは`Dockerfile`。計装オンのときはHTTPリクエストごとのspan(`opentelemetry-instrumentation-asgi`)も出す。`MCP_SERVER_OTEL=0`で計装オフ | ✅ |
| [system/mcp_client.py](../system/mcp_client.py) | SDKの`Client`を包み、tool呼び出しを`execute_tool` spanで囲む。HTTPヘッダにも`traceparent`を載せる(`httpx2`の`event_hooks`)。AgentCoreのURLならSigV4で署名し、`Mcp-Session-Id`を送り返す(§7の12)。`async with`の間は接続を使い回す | ✅ |
| [scripts/try_mcp.py](../scripts/try_mcp.py) | 手元の動作確認。ツール3つを「1つずつ」と「同時に」で呼び、waterfallを`out/`に出す | ✅ |
| [scripts/run_agent.py](../scripts/run_agent.py) | 手元の動作確認。仮の質問(§6の3)をエージェントに流し、質問ごとのwaterfallを`out/`に出す | ✅ |
| [scripts/gpu_prepare.sh](../scripts/gpu_prepare.sh)・[infra/gpu_prepare/](../infra/gpu_prepare/) | 仮のGPUサーバと、その中で動かす手順。イメージと重みを移し、本番と同じ経路で1回試す。ふだんは作らない。NVMeを使う形に直した後は動かしていない | ✅ |
| [infra/gpu.tf](../infra/gpu.tf)・[infra/gpu_boot.sh.tftpl](../infra/gpu_boot.sh.tftpl) | VPCの中のGPUサーバ(vLLM＋Collector)と、起動手順。`terraform apply -target=aws_instance.gpu`で、要るエンドポイントごと作る | ✅ |
| [system/serving/otel-collector.yaml](../system/serving/otel-collector.yaml) | Collectorの設定(§1) | ✅ |
| [system/llm/](../system/llm/) | OpenAI互換クライアント。ストリーミングで受け、chat spanにTTFTとエンジンの内訳(llama.cppの`timings`、vLLMの`metrics`)を記録する。断片で届くツール呼び出しを組み立てる。HTTPヘッダに`traceparent`を載せる | ✅ |
| [system/agent.py](../system/agent.py) | エージェントループ(§1の「エージェントの基本形」) | ✅ |
| [infra/](../infra/) | Terraform(VPC・RDS・踏み台・AgentCore Runtime・基盤側のspanの配送)。`infra/account/`はTransaction Search。`infra/artifacts/`はvLLMとCollectorのイメージ(ECR)と重み(S3)の置き場で、段階ごとに消さない | ✅ |
| `docs/runbook.md`・`traces/` | 手順書と根拠のトレース | ⬜ |

## 4. span設計

### 今あるもの

| span | 作るもの | 主な属性 |
|---|---|---|
| `invoke_agent` (root) | 自作(`agent.py`) | `gen_ai.agent.name`。上限で打ち切ると`error.type=max_turns_exceeded` |
| `retrieval` | 自作 | `gen_ai.retrieval.top_k` |
| `execute_tool <ツール名>` | 自作(`mcp_client.py`) | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `mcp.method.name`, `mcp.protocol.version` |
| `chat <モデル名>` | 自作(`llm/client.py`) | `gen_ai.usage.input_tokens` / `output_tokens`、`gen_ai.request.temperature`、`gen_ai.response.finish_reasons`、`llm.time_to_first_token_ms`(クライアント側のTTFT)、`llamacpp.prompt_n` / `cache_n` / `prompt_ms` / `predicted_n` / `predicted_ms`(llama.cppの内訳)、`vllm.queue_time_ms` / `time_to_first_token_ms` / `generation_time_ms` / `mean_itl_ms` / `tokens_per_second` / `cached_tokens`(vLLMの内訳) |
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

- AgentCoreの基盤側のspanが、自分のspanと1本のトレースにつながるか(2026-10-28に解決): つながる。span名は
  `AgentCore.Runtime.Invoke`(`aws.operation.name`が`InvokeAgentRuntime`、`latency_ms`・`session.id`付き)。
  クライアントが送った`traceparent`を親にし、コンテナの`POST /mcp`はその子になる。Runtimeのトレースを有効にするのは、
  CloudWatch Logsの配送(送り元`TRACES`、送り先`XRAY`)でTerraformから書けた(公式のSDKの例はmemoryとgatewayだけを挙げていた)。
  ヘッダの許可リストには`traceparent`・`tracestate`を入れた(入れない場合に届くかは試していない)
- VPCモードのとき、コンテナからCloudWatchにトレースを送る経路(2026-10-28に解決): `xray`のインターフェース型
  エンドポイント経由で届いた(NATなし)。実行ロールに`xray:PutSpans`が要る
- AgentCore Runtimeの課金(待機中にも費用がかかるか)
- 手元からCloudWatchにOTLPで送るときの受付口の仕様と署名の方法(2026-10-26に解決): 受付口は
  `https://xray.<リージョン>.amazonaws.com/v1/traces`で、OTLP/HTTP(protobuf)だけを受け、gRPCは受けない。署名はSigV4で、
  サービス名は`xray`。Transaction Searchが有効でないと受け付けない
- **AgentCore Observability**(2026-10-28に解決): `UNIFIED_TRACES_DESTINATION_ENABLED=false`で基盤側のspanもaws/spansに寄せ、
  `logs:PutResourcePolicy`は戻していない。ADOTは使わず、`DISABLE_ADOT_OBSERVABILITY=true`でRuntimeが入れる`OTEL_*`の
  環境変数を消した(標準のOTLPエクスポーターがヘッダなどを環境変数からも読むため)
- **AWSの画面でトレースを見る方法**(2026-10-31に解決): 索引の割合は1%のまま、トレースIDをX-Ray形式にして、画面の
  waterfallで見る。事例の記録に載せるトレースは、`StartTraceRetrieval`で取り出して自作のwaterfallで書き出す(段階6の前に作る)
  - トレースの一覧と`batch-get-traces`は、索引(トレースの要約)に入ったトレースだけを返す。索引はトレースIDごとに確率で選ばれる
    (初期値1%)ので、手元から流す少ないトレースはまず入らない(2026-10-27に0件)。基盤のヘルスチェックと思われる2spanの
    トレースが約2秒おきに入っていたが、確率で選ぶので、自分のトレースの枠を奪うわけではない
  - 索引に入っていないトレースも、トレースIDを指定すれば画面のトレースの詳細(waterfall)で開ける(スマホでも見えた)。
    ただしIDの先頭8桁を作成時刻(Unix時刻の16進数)として読むので、OpenTelemetryの既定のランダムなIDでは
    「有効期限が切れています」になる(先頭が2014年を指したID)。先頭を今の時刻にしたIDで送ると開けた
  - APIの`StartTraceRetrieval`→`ListRetrievedTraces`(`--trace-format OTEL`)では、時刻の範囲を指定すれば
    既定のIDのトレースも取り出せた(32spanすべて)
  - 閲覧用のIAMユーザーには、AWS管理ポリシー`CloudWatchReadOnlyAccess`が要る。画面は索引外のトレースを
    `xray:StartTraceRetrieval`で取り出しており、これが無いと開けない
  - 割合を上げる案は採らない。1%を超える分は索引に入れたspanの数で課金され、索引は条件(サービス名など)では絞れず
    割合しか指定できない。費用自体は小さい見込み(公式価格での概算で、Runtimeを1か月置いて100%にしても約2ドル。東京の単価は未確認)
- RDSで使えるpgvectorのバージョン(2026-10-25に解決): 東京のRDS for PostgreSQL 18系では0.8.1(18.1〜18.3)・
  0.8.2(18.4以降)。初期設定の18.3では0.8.1だった(§8)
- この PC の CPU で llama.cpp の小さいモデルがどれくらいの速さで動くか(ツール定義入りのプロンプトで)
- **AgentCoreのセッションID**(2026-10-28に解決): 2026-07-28版のMCPにはセッションが無いので、SDKのクライアントは送り返さない。AgentCoreの振り分けに要るので、自作のクライアントで送り返す(§7の12)
- **AgentCoreに載せるときのサーバ設定**(2026-10-28に解決): `stateless_http=True`、ホストは`0.0.0.0`、ARM64のコンテナ
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
- [x] 4. Terraform: VPC・RDS(pgvector)・AgentCore・CloudWatch。データ投入と、pgvectorの検索の計測
  - [x] 土台: 状態ファイルの置き場所(既存のバケット、別キー。バケット名は`infra/backend.hcl`に分けてコミットしない)、
    VPC・プライベートサブネット2つ(AZ IDで`apne1-az1`・`az2`)・S3のgatewayエンドポイント。作って消せることを確かめた(2026-10-12)
  - [x] RDS(pgvector): つなぎ方(§1)を決め、手元からつながることを確かめた(2026-10-25)。合成ベクトルを入れ、
    HNSWの有無で検索を測った(2026-10-26。`scripts/load_pgvector.py`・`scripts/measure_pgvector.py`、結果は§8)
  - [x] AgentCoreの受け皿: ECRのリポジトリ、実行ロール、Runtime用のセキュリティグループ(RDSへの許可も)、
    インターフェース型エンドポイント4つ。作って消せることを確かめた(2026-10-26)。Runtime本体は段階5で作る
  - [x] CloudWatch: Transaction Searchを`infra/account/`で有効にし(2026-10-26)、手元のエージェントのspanを送る
    `tracing/aws_exporter.py`を足した。仮の質問1つで、エージェント側のspan 9本がaws/spansに1本のトレースとして、
    親子関係を保ったまま届いた(2026-10-27。サーバ側のspanは手元のSQLiteだけ。§5の「AWSの画面でトレースを見る方法」)
- [x] 5. MCPサーバをAgentCoreに預け、トレースをCloudWatchに集める
  (エージェントと2Bは手元のまま。つながることと、spanが1本のトレースになることを確かめる)
  - [x] コンテナ(ARM64、`stateless_http`)、pgvectorでの検索(`RETRIEVAL_BACKEND`)、spanの送り先の切り替え(`TRACE_TO`)(2026-10-27)
  - [x] Runtime本体・基盤側のspanの配送・SigV4で署名するクライアント(2026-10-28)。仮の質問2で、エージェント・基盤・
    サーバのspanが1本のトレースとしてaws/spansに入った(エージェントの`MCP send`→`AgentCore.Runtime.Invoke`→`POST /mcp`)
  - [x] 検索をRDSに切り替え(2026-10-28)。MCPサーバは専用のDBユーザー(`rds_iam`、`documents`の読み取りだけ)にIAMのトークンで
    ログインし、SSLは`verify-full`。`SELECT documents`のspanが`tools/call retrieve_documents`の子として同じトレースに入った
  - [x] トレースIDをX-Ray形式にする(2026-10-31。`build_tracer_provider`で`AwsXRayIdGenerator`を使う)。画面の見方は§5。
    基盤側のspanがこのIDを引き継いで1本のトレースのままになるかは、5bでRuntimeを立てたときに確かめる
- [ ] 5b. GPUのEC2(vLLM＋Qwen3-8B、§1)を同じVPCに置き、エージェントをAgentCore Runtimeに移す。
  GPUのTerraformは`toolcall-serving-bench`のものを元にする(g6.xlargeは1時間約$1.17、月60ドルの予算は同じアカウントで分け合う)。
  `toolcall-serving-bench`は2026-10-30に終えた。作る前に決めること・作ること:
  - [x] エージェントからLLMへの呼び出しに`traceparent`を付ける(2026-10-31。親は`chat` span。エンジンが実際に引き継ぐかは5bで確かめる)
  - [x] 仮のGPUサーバ(公開IPつき)で、公式イメージをECRへ、重みをS3へ移す(2026-10-31。§1、`scripts/gpu_prepare.sh`)。コンテナからGPUが使え(Ubuntu 24.04版のDLAMI)、
    `transformers`はベンチと同じ5.17.0で、OTelの依存も入っていたので、イメージは公式のまま使う。実測は§8
  - [x] GPUのEC2をVPCに置く(2026-11-01、`infra/gpu.tf`)。起動すると推論サーバになる(`gpu_boot.sh.tftpl`)。コンテナはホストのネットワークで動かす(Collectorが認証情報を取れ、vLLMから`127.0.0.1`で送れる)。
    S3のエンドポイントのポリシーに重みのバケット、ECR・xray・SSMのエンドポイントのSGにGPUのSGからの受け入れを足した。vLLMへの受け入れはまだ無く、エージェントのRuntimeを作るときに足す
  - [x] vLLMが`traceparent`を引き継ぎ、Collector経由でspanがCloudWatchに届くこと(2026-11-01)。`llm_request` spanが、送った`chat` spanを親として同じトレースに入った。chat spanの`vllm.*`も実機で確認した(§8)
  - [x] g6のAZ(2026-10-31)。g6.xlargeは`apne1-az1`(=1c)と`apne1-az4`にあり、今のサブネット(`apne1-az1`)に置ける。容量不足のとき
    (ベンチでは約1.5時間続いたことがある)はAZを変えず、待って再試行する。AZをまたぐ遅れを検索に混ぜないため。起動手順は再試行しないので、エンドポイントを先に作り、GPUを最後に作る(容量不足で失敗しても、消すまで1時間約$0.10)
  - [x] 文脈の長さ(`--max-model-len`)は16384(2026-10-31)。KVは22,976トークンで載った(§8)。ベンチの8192は引き継がない(§7の9で既に上限に当たった)。
    モデルの上限40,960はKVに載らない。ツール定義1,360トークンに、ツール結果100行(1行50トークンと仮定し約5,000)が2回入ると8192を超える
  - [x] vLLMの応答の内訳をchat spanに記録する(2026-11-01)。ストリーミングでは最後のチャンクの`metrics`
    (`queue_time_ms`・`time_to_first_token_ms`・`generation_time_ms`・`mean_itl_ms`・`tokens_per_second`)と`usage.prompt_tokens_details.cached_tokens`にある。
    テストは偽の応答で、形は仮のGPUサーバの実際の応答に合わせた。実機でspanに載ることはGPUのEC2を置いたときに確かめる。
    spanの`gen_ai.provider.name`は`llama.cpp`固定のままなので、エージェントをRuntimeへ移すときに接続先の既定値と一緒に直す
  - [x] エンジンの起動フラグ: `--enable-per-request-metrics`・`--enable-prompt-tokens-details`(2026-10-31、応答に内訳が入ることを確認)。vLLMの`time_to_first_token_ms`は
    待ち行列の時間を含まないので、クライアント側のTTFTとは別の名前で記録する。Prometheusの`/metrics`はCloudWatchに送らず、
    事例の前後で保存する
- [ ] 6. 事例を1つずつ再現し、手順書に書き、トレースを書き出す。事例ごとに「症状 → 最初に立てた仮説 → 決め手になったspan・属性」の
  形で書く(§7の2・12の書き方)。#3・M2は§7の4・9・12で実際に起きたので、再現し直さずその記録から書く。#12の並行数は、
  想定する利用者の数から先に決める

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
    llama.cppでは確かめていない。**vLLMでは同じ形の差を確かめた(2026-11-01、§8)**: ツールを呼ぶ応答では、クライアントのTTFTが約600ms、
    vLLMの`time_to_first_token_ms`が約65ms。サーバの中で生の応答を見ると、最初の空のチャンクが94ms、ツール名の入った最初のチャンクが570msで、
    その間はトークン8個分(1個約60ms)にあたる。v0.31.0の`hermes_tool_parser.py`は、`"name": "…"`が閉じるまで何も返さない(`_extract_tool_name`)ので、この待ちはパーサによる。生成そのものは止めないので、応答全体の時間は延びない。
    つまりツールを呼ぶ応答のクライアントのTTFTには、入力の長さ(#8)と関係のない約500msが混ざる(ツール名が長いほど増える)。#8の切り分けでは`vllm.time_to_first_token_ms`を見る
11. **CloudWatchへの同期の送信は、中身の無い親spanを537msにした。** 試しのスクリプトで、親spanの中で子spanを1本作って
    閉じただけで、`SimpleSpanProcessor`では親が537msになった。子spanが終わるたびに、署名とHTTPSの送信を呼び出し元で
    待つため。2(SQLiteの約67ms)と同じ形で、1桁大きい。`batch=True`では起きない。手元からAWSまでの往復が含まれるので、
    同じリージョンの中から送れば小さくなると推測しているが、確かめていない
12. **AgentCore上のMCPサーバへのリクエストが、毎回コールドスタートしていた。** `try_mcp.py`が31秒かかり、基盤側のspanを
    並べると、8回のリクエストがすべて別の`session.id`で、うち4回は`latency_ms`が5〜7秒だった(コンテナの`POST /mcp`は数ms)。
    2026-07-28版のMCPにはセッションが無く、SDKのクライアントは仕様どおりIDを送らない。一方でAgentCoreの振り分けは
    `Mcp-Session-Id`に頼っており、レスポンスに付けたこのIDを送り返さないと、リクエストごとに別のmicroVMへ振り分ける
    (AWSの文書はクライアントに送り返しを求めている)。プロトコルの版と基盤の前提の組み合わせで起きる食い違いで、どちらかの不具合ではない。
    クライアントで送り返すようにすると、8回とも同じセッションになり、31秒が5.6秒、基盤側は24〜989msになった(2026-10-28)。
    サーバ側の計装だけでは見えず、基盤側のspanの`session.id`と`latency_ms`を並べて初めて分かった

## 8. 実測済みの前提値

- PCメモリ総量 13.74GB。大きな配列を作る前に空きを確認する
- 手元のfaiss(2026-10-04以前、Windows側。手元で動かすための代わりの部品で、事例の値ではない): `hnswlib`は
  Python 3.13/Windowsでprebuilt wheelが無かったため`faiss`を使った。全件走査はコーパスの大きさにほぼ比例
  (約8MBで1ms)。128次元×50万件・1回の検索でサブクエリ25回は、遅さが見えるよう逆算した値。HNSWの
  `efSearch`がfaissの既定値16だと、約1%のクエリでrecallが0になった(64で解消)
- venv: WSLの`~/.venvs/agent-latency-tracer`(Python 3.14.4、faiss 1.15.1。2026-10-06作成、全テスト通過)。
  Windows側の`.venv/Scripts/python.exe`(3.13)はfaissが読み込めないため、faissを使わない作業にだけ使える
- 2026-10-06時点で、このPCではWindowsのスマートアプリコントロールがfaissのDLLの読み込みを止める。faissを読み込む
  テスト(`test_retrieval.py`・`test_mcp.py`)とMCPサーバは、Windows側では動かない(10-04までは動いていた)。対処は手元の
  実行環境をWSLに移すこと(§1)。2aのSQLiteの値・READMEのfaissの実測値はWindows側での値
- WSL2が使えるメモリは初期設定で約6GB(PCの半分)
- llama.cppのコンテナは、モデルをWSLのUbuntuのホーム(`/home/<ユーザー>/models`)からマウントする。Ubuntuが起動する前に
  Docker Desktopを起動してコンテナを動かすと、モデルのファイルが見つからずに終了した。Ubuntuを起動してから動かし直すと読み込めた
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
- RDS(2026-10-25、db.t4g.micro、パラメータは初期設定): PostgreSQL 18.3(aarch64)、pgvector 0.8.1。`shared_buffers`約180MB、
  `effective_cache_size`約360MB、`maintenance_work_mem` 64MB、`work_mem` 4MB、`max_parallel_maintenance_workers` 2
- Terraform(土台＋踏み台＋SSM＋RDS＋AgentCoreの受け皿、36個、2026-10-26): applyは約6分、destroyは約5分
- Terraform(土台＋踏み台＋SSMのエンドポイント＋RDS、23個): applyは約7分(RDSが5分42秒)、destroyは約4分(インターフェース型
  エンドポイントの削除が約3分)
- SSMのトンネル: `start-session`を始めてから手元のポートが開くまで約1分。開く前につなぐと失敗する
- **pgvectorの検索(2026-10-26、db.t4g.medium、PostgreSQL 18.3・pgvector 0.8.1、1024次元×10万件、§1の条件)**:
  - HNSWなし(Seq Scan): 中央値488ms、p95 625ms。HNSWあり(Index Scan): 中央値1.0ms、p95 1.5ms
  - recall@5は0.882(`ef_search`は初期値の40のまま。全件走査の結果を正解とした)
  - HNSWを作るのに87秒(`maintenance_work_mem`はその接続だけ1GB)、インデックスは781MB(見込みの約2倍)。表は533MB
  - 両方ともディスクからの読み込みは0で、メモリに収まっていた。`shared_buffers`約910MB、`effective_cache_size`約1.8GB
  - 投入はトンネル越しに600秒(約0.9MB/秒)。apply約6分、destroy約6分(インターフェース型エンドポイントの削除が約5分)
- **仮のGPUサーバ(2026-10-31、g6.xlarge・1c、Ubuntu 24.04版のDLAMI 20261030、vLLM 0.31.0、Qwen3-8B bf16)**。apply〜destroyは1時間37分(約$1.9)。
  確認の途中で権限待ちになった時間を含む。時間は1回ずつの値:
  - 重みの読み込み: **ルートのEBS(gp3 100GB、初期設定)で580秒、インスタンス付属のNVMe(233GB)で42秒**。起動全体は、NVMeで203秒、EBSで約13分。
    EBSでは1シャードに約2分かかった。遅い理由は調べていない
  - KV: 22,976トークン(`Maximum concurrency for 16,384 tokens: 1.40x`)。EBSからとNVMeからの2回とも同じ。読み込み後のGPU使用は23,034MiBのうち19,832MiB。
    ベンチの通常の値30,240ではなく、ベンチが新しいインスタンスの最初の起動でだけ見た値(22,992)に近い。2回目の起動でも少ないので「最初だけ」では説明できない。原因は分かっていない
  - 転送: イメージの取得約6分、ECRへのpushと取り直し約6分、重みのHFからの取得2分10秒とS3へのアップロードで合計約18分、S3から1分47秒(ルートのEBSへ)
  - ECRの大きさ: vLLM 9.04GB、Collector 0.12GB。重みはS3に16.4GB、15ファイル。保管は合わせて月約$1.3
  - 応答(ツール1つ、162トークン): TTFT 84ms、`queue_time_ms` 0.02〜0.05ms、`mean_itl_ms`約60ms、2回目は`cached_tokens` 160でTTFT 65ms。
    約60msは、ベンチのTPOT(並行数1のp50)61.4msと同じ水準
  - `xray:PutSpans`だけでは、OTLPの受付口が403を返した(`not authorized to perform: xray:PutTraceSegments`)
- **出口の無いVPCのGPUサーバ(2026-11-01、g6.xlarge・1c、イメージと重みはECR/S3から)**。destroyに11分半。GPUは約25分動かし、エンドポイントと合わせて約$0.6(概算)。時間は1回の値:
  - 起動から応答できるまで12分半。起動手順の開始まで1分半、イメージの取得と重みのコピーで約6分、vLLMの起動で約4分半。KVは今回も22,976トークン
  - トレース: 手元の`LlmClient`からSession Managerのポート転送で呼び、2本とも`chat`(手元)→`llm_request`(vLLM)が同じトレースに入った。Collectorのログにエラーは無かった。
    `llm_request`は`vllm.tracing.otel`が出すSERVERのspanで、`gen_ai.latency.time_in_queue`・`time_in_model_prefill`・`time_in_model_decode`・`e2e`などを持つ
  - `chat`の親に、手元のサービス名で同じ長さの`InternalOperation`というspanが`aws/spans`に入っていた。自作のコードには無いspanで、手元の記録(in-memory)にも無い。誰が足したかは調べていない
  - TTFT(各3回、ポート転送越し): 文章の応答はクライアント118〜194ms・vLLM 61〜71ms、ツールを呼ぶ応答はクライアント596〜623ms・vLLM 64〜65ms(§7の10)
- **AgentCore Runtime(2026-10-28)**:
  - イメージ(ARM64、python:3.14-slim)は約121MB。手元(QEMUで代わりに実行)でのビルドとpushに6〜7分
  - Runtimeの作成に4分48秒。作り直さなくても、環境変数を変えるとその場で更新される
  - VPCモードでS3のゲートウェイ型エンドポイントからイメージを取るとき、RuntimeのSGにS3のプレフィックスリスト宛ての443番の
    出口が要る(ゲートウェイ型はS3の公開IPのまま通る)。無いと、ログが1行も出ないまま、最初の呼び出しが
    「Runtime initialization time exceeded(120秒)」で失敗した
  - AgentCoreは定期的にコンテナへ死活確認を送り(HTTPの`GET /ping`と、MCPの`ping`)、そのspanもaws/spansに入る。
    手元のtrace_idで絞らないと、自分のトレースが埋もれる
  - 1回のツール呼び出し(仮の質問2): エージェントの`MCP send`約1.16秒、基盤側約0.86秒、サーバの`POST /mcp`約0.80秒
    (ツールの中身はモックの待ち時間)。手元から東京リージョンまでの分が約0.3秒
  - RDSへの検索(1,000件、HNSWなし): `SELECT documents`約8ms
  - destroy: インターフェース型エンドポイントの削除がTerraformの待ち時間(10分)を超えてエラーになった(その後に消えた)。
    また、Runtimeを消した後も、AgentCoreが作ったネットワークインターフェース(`agentic_ai`型)がRuntimeのSGをつかんだまま残り、
    SG・サブネット・VPCを消せなかった。公式には、Runtimeを消した後もネットワークインターフェースが最大8時間残る。
    時間をおいてdestroyし直す(どれも無料)
