# agent-latency-tracer

LLM・MCP経由のツール・RAGを持つPython製のAIエージェントで**踏みがちな遅延を再現**し、
それぞれを**切り分けるには何を記録しておけばよいか**を確かめるプロジェクト。
MCPサーバはAmazon Bedrock AgentCore Runtime上で動かし、計装はOpenTelemetryの規約に乗る。

## 動機

業務で作っていたAIチャットの応答が遅かったが、ログが何も残っておらず原因を追えなかった。
同じ状況を繰り返さないため、よくある遅延の原因と、それを見分けるのに要る計装の対応を、
自分の手で再現して確かめておく。

## 成果物

- **切り分けの手順書**([docs/runbook.md](docs/runbook.md)、作成予定): 事例ごとに
  「waterfallでどう見えるか → 見るべきspan・属性 → 考えられる原因 → 区別のつけ方」をまとめる
- **根拠のトレース**([traces/](traces/)、作成予定): 手順書の各行の根拠として、実際に取ったトレースを
  JSONで書き出し、waterfallの画像と一緒に置く(AWSアカウントIDやARNは伏せる)。AWSを使わずに
  結果を確かめられるようにするため
- **計装**: エージェント・MCPクライアント/サーバ・LLM呼び出し・検索へのspan設計
- **インフラのコード**: Terraformで、使うときに作り、終わったら消す

## 進め方

1. 一般に踏みがちな遅延の落とし穴を、出典つきで集める([docs/latency-pitfalls-survey.md](docs/latency-pitfalls-survey.md))
2. 1本のエージェントの中で、1か所だけを落とし穴ありに切り替えて再現する。落とし穴ありの書き方と
   直した書き方は、事例ごとに1つのファイルに並べて書く
3. waterfallで見え方を確かめ、手順書に書く
4. 見分けられないものは、何を記録すれば見分けられるかを考えて計装を足し、足す前と後を比べる

見分けられなかったものも手順書に書く。動かしている途中で自然に起きた遅延(小さいLLMの思わぬ動きなど)も、
「自然に発生」として手順書に加える。

## スコープ外

- **運用改善の実績ではない。** トラフィックは自前で用意した合成トラフィック
- **未知の原因を見つける仕組みではない。** 扱うのは事例集と、動かしている途中で遭遇したものの範囲
- **応答品質は測らない。** 速度と引き換えに品質が変わりうる修正は、安価に測れるもの
  (#5のrecall@k)だけ測り、それ以外は「品質は未計測」と書く

## 構成

```
手元のPC              AWS(東京リージョン)
┌────────────┐       ┌──────────────────────────────────────────────────────┐
│ 利用者役の    │       │ VPC                                                  │
│ スクリプト    │──────▶│ AgentCore Runtime: エージェント(Python)               │
└────────────┘ SigV4 │   ├ chat ─────────▶ GPUのEC2: 推論サーバ(Qwen3-8B)   │
                     │   └ MCP(SigV4) ──▶ AgentCore Runtime: 自作MCPサーバ    │
                     │                       └ 文書検索 ──▶ RDS + pgvector     │
                     │ CloudWatch(トレース・メトリクス)                        │
                     └──────────────────────────────────────────────────────┘
```

開発中は、エージェントとLLM(llama.cpp、Qwen3.5-2B)を手元で動かす。

| 部品 | 実装 | 補足 |
|---|---|---|
| エージェント | Python・asyncio。AgentCore Runtimeで動かす | LLMがツール(業務ツール・文書検索)を選んで呼び、結果を見て次を決めるループ |
| LLM | GPUのEC2上の推論サーバ(OpenAI互換API)で、Qwen3-8B。開発中は手元のllama.cppでQwen3.5-2B | 手元のCPUではLLMが時間のほとんどを占め、ツールや検索の遅れが埋もれるため、事例の再現はGPUで行う。OpenAI互換なので、接続先を変えるだけで差し替えられる |
| ツール | 自作のMCPサーバ(公式Python SDKの低レベルAPI、Streamable HTTP) | 業務ツール4つ(データの種類を引数で指定する汎用型)とモック実行(200〜800ms待ってから、小さな合成データを返す)、文書検索ツール。AgentCore Runtimeに預けて動かす |
| 認証 | IAMの署名(SigV4) | 手元のAWSクレデンシャルで署名する |
| RAG | RDS for PostgreSQL + pgvector | 検索はMCPサーバのツールの1つで、使うかどうかはLLMが決める。RDSはVPCの中だけに置き、インターネットに出さない |
| 計装 | OpenTelemetry(GenAI規約・MCP規約) | トレースの文脈はMCPリクエストの`params._meta.traceparent`でサーバに渡す |
| 保存・可視化 | CloudWatch(Transaction Search) | AgentCoreが出す基盤側のspan・メトリクスと、自分で張ったspanを同じ場所に集める |

**手元だけでも動く。** MCPサーバとエージェントはAWSを使わずに手元で動かせる(検索はfaiss、トレースは
自作のSQLite保存と単一HTMLのwaterfallビューア)。開発中の確認と、AWSを使わずにコードを試したい読み手のため。

```bash
# Linux(WindowsではWSL)で。venvは ~/.venvs/agent-latency-tracer に作り、pip install -e ".[dev]"
# トレースの保存先は、測定中の書き込みが遅くならないよう、Linux側のディスクに置く
export TRACE_DB=~/traces/traces.db
# ターミナル1: MCPサーバ(127.0.0.1:8000/mcp)
~/.venvs/agent-latency-tracer/bin/python -m system.mcp_server
# ターミナル2: ツールを3つ「1つずつ」と「同時に」呼び、waterfallをout/に出す
~/.venvs/agent-latency-tracer/bin/python scripts/try_mcp.py
# ターミナル2: 仮の質問をエージェントに流し、質問ごとのwaterfallをout/に出す
# (llama.cppを127.0.0.1:8080で起動しておく。1問20〜60秒ほど)
~/.venvs/agent-latency-tracer/bin/python scripts/run_agent.py
```

**MCPサーバ側の計装はオン/オフを切り替えられるようにする。** サーバが別の言語・別のチームで作られていて
計装が無い、という状況はよくあるため。

**AWSで再現する場合**: Terraformで環境を作り、使い終わったら消す。費用の目安と消す手順は、インフラを
作る段階でここに書く。

## 事例集

番号は[調査メモ](docs/latency-pitfalls-survey.md)の番号。

| # | 落とし穴 | 修正 |
|---|---|---|
| 1 | 独立したtool呼び出しを逐次awaitする | `asyncio.gather`で並列化 |
| 2 | async関数の中でブロッキング呼び出し(gatherしても実質直列) | 非同期I/O/スレッドへ逃がす |
| 3 | エージェントループの過剰反復(同じtoolを何度も呼ぶ) | 反復回数の上限 |
| M2 | リクエストごとにMCPクライアントを作り直す(接続・版の問い合わせ・ツール一覧の取り直し。AgentCoreではセッションIDを使い回せず、新しいmicroVMのコールドスタートを待つ) | クライアントの再利用 |
| R | MCPのタイムアウト→リトライが1つのspanの中に隠れる | リトライの各試行を記録する |
| 5 | ベクトル検索のインデックスが無く全件走査になる(pgvector) | HNSWインデックスを作る |
| 6 | 検索を1件ずつ呼ぶ | まとめて呼ぶ(問題になるかは実測で確かめる) |
| M1 | tool定義の多さによるプロンプトの膨張 | 必要なtoolだけ渡す/遅延ロード |
| M4 | 大きなtool結果が次のターンのコンテキストに積まれる | 結果の要約・切り詰め |
| 8/9 | 長い入力(prefill)と長い出力(decode) | 入力の絞り込み/出力長の制御 |
| 10 | プレフィックスキャッシュが効かない(先頭に日時等の動的値) | 動的値を後ろへ移す |

**候補(事例を選ぶときに採否を決める)**: リクエストのたびに認証や署名の準備をやり直す。
動かしている途中で見つかったものは[設計書](docs/DESIGN.md)の§7。

### 検索についての実測メモ(事例5、RDSのpgvector)

RDS for PostgreSQL(db.t4g.medium)のpgvectorで、同じ表に対して「HNSWなし」と「HNSWあり」を測った値
(2026-10-26)。条件(1024次元×10万件、top-5)は、測る前に現実の値から決めた。根拠と詳しい値は[設計書](docs/DESIGN.md)の§1・§8。

| | HNSWなし(全件走査) | HNSWあり |
|---|---|---|
| DBの中の実行時間(中央値) | 488ms | 1.0ms |
| p95 | 625ms | 1.5ms |

- HNSWのパラメータはpgvectorの初期値のまま。取りこぼし(recall@5)は0.882。全件走査の結果を正解としたので、
  追加のラベル付けは要らない
- 表(533MB)とインデックス(781MB)はメモリに収まっていた(ディスクからの読み込みは0)

## ディレクトリ構成

```
agent-latency-tracer/
├── system/                 # 観測対象のエージェント
│   ├── tools/               # 業務ツールの定義、データの種類と項目、合成データ、モック実行
│   ├── retrieval/            # 手元用の検索(faiss)
│   ├── mcp_server/            # 自作MCPサーバ
│   ├── mcp_client.py          # 計装つきMCPクライアント
│   ├── agent.py               # エージェントループ
│   └── llm/                   # OpenAI互換クライアント
├── tracing/                  # 計装と、手元用の保存・可視化
│   ├── instrumentation.py    # OTel span定義
│   ├── exporter.py            # SQLiteへのexporter(手元用)
│   └── viewer/waterfall.py     # waterfallビューア(手元用)
├── scripts/                  # 手元での動作確認
├── infra/                    # Terraform(作成予定)
├── traces/                   # 手順書の根拠にするトレース(作成予定)
├── tests/
└── docs/
    ├── DESIGN.md               # 設計書(現状と次にやること)
    ├── latency-pitfalls-survey.md  # 事例の調査メモ(出典つき)
    ├── tool-catalog-survey.md     # 業務ツールの形を決めるための調査メモ
    └── runbook.md              # 切り分けの手順書(作成予定)
```

業務ツールは、公開されている業務システムのMCPサーバに多い汎用型にした。データの構造を調べる`describe_data`、
探す`find_records`、1件取る`get_record`、集計する`aggregate_records`の4つで、データの種類(販売・購買・在庫・会計・
人事と共通マスタの15種類)は引数で指定する。種類と項目は`system/tools/data_schema.json`、モックが返す合成データは`system/tools/mock_data.json`。決めた経緯は
[設計書](docs/DESIGN.md)§6の2bと[調査メモ](docs/tool-catalog-survey.md)。
