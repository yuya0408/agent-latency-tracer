# 調査メモ: LLMエージェントで踏みがちな遅延の落とし穴

事例の一覧([手順書](runbook.md#事例の一覧))の候補と出典。選定基準と採否は、事例を選んだ時点(2026-10〜11月)のもの。
作る途中で自然に起きた件は、この基準とは別に手順書に載せた(基盤側の問題を含む)。

**出典の注意**: Web検索で集めた段階のものを含む。一次情報(公式ドキュメント・論文・issue)は、
その事例を手順書に書く前に本文を読んで裏を取る。ベンダーブログ由来の数値は根拠に使わない。

## 選定基準

1. 一次情報の出典が最低1つある
2. 原因がアプリケーション側にある(推論サーバ内部・基盤側の問題は対象外)。ただし推論サーバの待ち行列(#12)は、
   自前ホストのエンジンが待ち行列の時間をspanと応答に出すので、アプリから切り分けられるとして加えた(2026-10-31)
3. 件数は11件程度まで(入れる落とし穴の数。自然に起きた件を含めた手順書の事例は22件)

## 候補

### エージェント・検索・生成

| # | 落とし穴 | 主な出典 | 採否 |
|---|---|---|---|
| 1 | 独立したtool呼び出しを逐次awaitする | LLMCompiler (arXiv 2312.04511) | 採用 |
| 2 | async関数の中でブロッキング呼び出し(`requests`・`time.sleep`等)→ gatherしても実質直列 | Python公式 asyncio-dev ドキュメント | 採用 |
| 3 | エージェントループの過剰反復(同じtoolを何度も呼ぶ) | arXiv 2607.01641(infinite agentic loops) | 採用 |
| 4 | レート制限(429)→リトライ/バックオフ | OpenAI/Anthropic のrate limitドキュメント(要確認) | MCPのタイムアウト→リトライ(R)に置き換え。ローカルLLMでは429が起きないため。Rものちに外した(2026-11-10。MCPのSDKと主なフレームワークが既定で再試行しないため)。既定で再送するAWSのSDKの形は、自然に起きた「同じ質問が2重に処理される」で扱った |
| 5 | 全件走査のベクトル検索 | faiss ドキュメント(要確認) | 採用 |
| 6 | 埋め込み/検索を1件ずつ呼ぶ(バッチ化しない) | open-webui Discussion #13966 | 不採用(2026-10-31。pgvectorのHNSWありで1件1msのため、まとめても差が出ない見込み) |
| 7 | リランカーにかける候補数が多すぎる | arXiv 2502.19280 | 不採用(余力が無かった。実験は終えた) |
| 8 | 長コンテキスト(prefill) | Red Hat「5 steps to triage vLLM performance」 | 採用 |
| 9 | 出力トークン過多(decode) | OpenAI「Latency optimization」ガイド | 採用 |
| 10 | プロンプトキャッシュが効かない(先頭に日時等の動的値) | OpenAI/Anthropic の prompt caching ドキュメント | 採用 |
| 11 | HTTPクライアントを毎回作り、接続を再利用しない | GitHub issue(cadis #1776、openJiuwen-ai/agent-core #17) | 不採用(HTTP層の計装が別途要る) |
| 12 | 推論サーバの待ち行列(負荷時) | Red Hat 同上 | 採用(2026-10-31。選定基準2の但し書き) |
| 13 | コールドスタート(モデル読み込み) | arXiv 2606.07362 | 不採用(推論サーバ側) |

### MCP

| # | 落とし穴 | 主な出典 | 採否 |
|---|---|---|---|
| M1 | tool定義が多すぎてプロンプトが膨らむ(毎ターンprefillが重い) | Anthropic「Code execution with MCP」、MCP仕様 SEP-1576(token bloat) | 採用 |
| M2 | リクエストごとにMCPセッションを張り直す(stdioならプロセス起動から) | anthropics/claude-code issue #82952 ほか(要一次情報確認) | 採用。手順書のM2は、作る途中で自然に起きた件(MCPのセッションIDを送り返さない)から書いた |
| M3 | ターンごとに`tools/list`を取り直す | 要調査 | M2に統合 |
| M4 | tool結果が大きく、そのまま次ターンのコンテキストに積まれる | anthropics/claude-code issue #45770 ほか | 採用→のちに外した(2026-11-06。確かめられることが#8と重なるため) |

## MCPの計装規約(一次情報で確認済み)

- OTel GenAIのMCP規約: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/mcp.md
  - span名は`{mcp.method.name} {target}`(例: `tools/call search_master`、`initialize`)
  - 必須: `mcp.method.name`。推奨: `mcp.session.id`、`mcp.protocol.version`、`network.transport`等
  - tool呼び出しは`gen_ai.operation.name = execute_tool`で、GenAIの`execute_tool` spanと互換。
    外側のGenAI計装が既にtool実行のspanを張っているなら、MCP側は別spanを作らず属性を足すべき(SHOULD NOT)
  - トレースコンテキストはMCPリクエストの`params._meta`に`traceparent`等で載せて伝搬する
  - metrics: `mcp.client.operation.duration`、`mcp.client.session.duration`等
  - 安定度はDevelopment(実験段階)
- MCP Python SDKは、サーバ側で受けた全メッセージにspanを自動で張る。クライアント・サーバが
  両方SDKならトレースコンテキストも自動伝搬: https://py.sdk.modelcontextprotocol.io/run/opentelemetry/
- 公式Go SDK(modelcontextprotocol/go-sdk)にはOTelの組み込みが見当たらず、サードパーティの
  ミドルウェア(https://github.com/olgasafonova/mcp-otel-go)がある程度。言語によっては
  サーバ側spanが得られないことがある

## サーバ側に計装が無い場合の論点

- MCPサーバが別の言語・別のチームで作られていて、クライアント側しか計装できないことはよくある
- それでもクライアントから`params._meta`に`traceparent`を載せておけば、将来サーバ側に計装が入った時に
  トレースがつながる
- クライアント側のspanだけで「サーバ内で遅い / 通信で遅い / クライアントで遅い」をどこまで
  切り分けられるか。できない部分は、サーバ側で何を計測すべきかの根拠になる

## 出典URL

- LLMCompiler: https://arxiv.org/pdf/2312.04511
- Python asyncio-dev: https://docs.python.org/3/library/asyncio-dev.html
- Infinite agentic loops: https://arxiv.org/html/2607.01641v1
- open-webui RAG最適化の議論: https://github.com/open-webui/open-webui/discussions/13966
- Reranker bottleneck: https://arxiv.org/pdf/2502.19280
- Red Hat vLLM triage: https://developers.redhat.com/articles/2026/03/09/5-steps-triage-vllm-performance
- OpenAI Latency optimization: https://developers.openai.com/api/docs/guides/latency-optimization
- OpenAI Prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
- Anthropic Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- 接続を再利用しない問題: https://github.com/Growth-Circle/cadis/issues/1776 , https://github.com/openJiuwen-ai/agent-core/issues/17
- Cold start in vLLM: https://arxiv.org/pdf/2606.07362
- Code execution with MCP: https://www.anthropic.com/engineering/code-execution-with-mcp
- SEP-1576: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1576
- claude-code #82952: https://github.com/anthropics/claude-code/issues/82952
- claude-code #45770: https://github.com/anthropics/claude-code/issues/45770
