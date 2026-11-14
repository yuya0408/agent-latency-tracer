# ADR-0001: MCPサーバとエージェントをAgentCore Runtimeに置き、GPUのサーバとは同居させない

- Status: Accepted
- Date: 2026-10-03

## 結論(Summary)

- MCPサーバは、AgentCore Runtimeに置く。呼び出しはIAMの署名(SigV4)で認証する。
- エージェントも、段階5b以降はAgentCore Runtimeに置く。MCPサーバとは別のRuntimeにし、GPUのEC2とは同居させない(A1)。
- 利用者役の手元のスクリプトが、`InvokeAgentRuntime`でエージェントを呼ぶ(A1)。
- AgentCoreは土台として使い、ほかの置き場所との比較(測定)はしない。

## 背景と問い(Context and Problem Statement)

再現したいのは、自前ホストのLLM・MCP経由のツール・RAGを持つエージェントの遅延。MCPサーバはHTTPでつなぐ形にし、
クラウドの上に置く。RAGのDB(RDS)はVPCの中だけに置くので、MCPサーバはVPCの中からDBにつなげる必要がある。

問い: MCPサーバ(と、後でエージェント)を、どこで動かすか。

## 検討した案(Considered Options)

| 案 | 採否 | 理由 |
| --- | --- | --- |
| AgentCore Runtime | 採用 | AWSのMCPサーバの配置ガイドが挙げる選択肢の1つ。セッションの分離・基盤のspanが組み込みで、VPCモードでRDSにつなげる |
| ECS(Fargate)に常駐させる | 却下 | コンテナが常駐し、状態を持たないMCPサーバならどのコンテナでも答えられる。セッションごとの起動が無いので、起動の遅れはコンテナを増やすときだけ。一方で、エージェント向けの機能(セッションの分離・基盤のspan)は自分で組むことになり、AWSのエージェント向けのサービスを使う経験という目的に合わない |
| Lambda | 却下 | 実行環境を新しく作るときに起動の遅れが入る。同期の呼び出しは15分まで、状態は外に置く必要がある |
| 手元のまま | 却下 | 通信の遅れ・基盤の遅れ(起動の遅れなど)が再現できない。手元で動く形は開発用に残す |
| エージェントをGPUのEC2に同居させる(A1) | 却下 | 推論サーバ(GPU)とエージェント(CPUのアプリ)は、役割も増やし方も違う。分けて置き、同じVPCの中から呼ぶのが一般的な形 |

ECS・Lambdaとの違いは、公式の文書で確かめたもの(2026-11-03)で、測って比べてはいない。

## 決定(Decision Outcome)

- MCPサーバ: AgentCore RuntimeのVPCモード(ネットワークの詳細はDESIGN §1「AgentCore Runtimeのネットワーク」。A3)。状態を持たない作り(`stateless_http`)にする。
  認証はSigV4にする。手元のクレデンシャルで済み、Cognito(ユーザー管理)が要らない。
- 段階5まではエージェントを手元に置く。MCPサーバの移行と分けて確かめるため。
- エージェント(A1): 段階5bでAgentCore Runtimeへ移す。ロールは、自分のイメージの取得とMCPサーバのRuntimeの呼び出し、AgentCore Memoryの読み書き(A2)だけで、RDSの権限は持たない。

## 影響(Consequences)

- 得るもの: 基盤のspan(`AgentCore.Runtime.Invoke`)が、自作のspanと同じトレースに入る(段階5・5bで確認)。
- 失うもの:
  - セッションごとに専用のmicroVMを起動する作りなので、新しいセッションの最初の呼び出しと、使わない時間(初期設定で15分)の後の呼び出しには、起動の遅れが入る。常駐の形なら入らない。
  - イメージはARM64で作る必要があり、ビルドとpushに1つ約10分かかる。
  - Runtimeを消した後、ネットワークインターフェースが最大8時間残り、VPCをすぐには消せない(DESIGN §8)。
- 残る穴: 専用のインスタンスで動かす種類(Instances)は調べていない。新しいセッションの最初の質問には、起動の遅れ(約1秒)が乗る。

### 妥当性への脅威(Threats to Validity)

| 懸念 | 対処 | 残る影響 |
| --- | --- | --- |
| セッションの起動の遅れが、事例の遅れに混ざる | 事例で測るときは、セッションIDを固定し、測る前に1周流して温める。基盤の分は、`AgentCore.Runtime.Invoke`と`invoke_agent`の差で分ける | 新しいセッションの最初の質問では約1秒(実測、DESIGN §7の16。A3)。MCPのセッションIDを送り返さないと呼び出しごとに5〜8秒(§7の12、手順書のM2) |

## 参考(More Information)

- DESIGN §1「MCPサーバの置き場所」「エージェントの置き場所」「認証」、§6の5・5b、§7の12、§8の「AgentCore Runtime」
- AWS: [Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
  (セッションごとのmicroVM、同じセッションIDを使わないと起動の遅れが入りうる)、
  [Configure lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)
  (使わない時間の初期値900秒、最長28800秒)、
  [Deploy MCP servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
- AWS: [MCP Deployment Patterns on AWS](https://docs.aws.amazon.com/prescriptive-guidance/latest/mcp-deployment-patterns-on-aws/introduction.html)
  (AgentCore・Lambda＋API Gateway・ECS・EKS・EC2の5つ)、
  [Lambda + API Gateway](https://docs.aws.amazon.com/prescriptive-guidance/latest/mcp-deployment-patterns-on-aws/deployment-pattern-2-aws-lambda-amazon-api-gateway.html)
- 2026-11-03に、DESIGN §1から移した。ECS・Lambdaの行は、このとき公式の文書を確かめて足した(10-03の時点では検討していない)。

## 追記(Amendments)

種類: **明確化**=書き方だけを直した/**規則の追加**=その規則で読む結果を見る前に決めた/**結果の後の補完**=その規則で読む結果を見た後に決めた(理由を本文に書く)/**適用の記録**=決めたとおりに読んだ結果。結果を見た後に決めたことそのものを変えるときは、新しいADRを立ててこのADRをSupersededにする。

| 番号 | 日付 | 見ていた結果 | 種類 | 中身 | 判定への影響 |
| --- | --- | --- | --- | --- | --- |
| A1 | 2026-10-14 | 手元のCPU・2Bで1問18〜63秒(ADR-0003) | 結果の後の補完 | エージェントも段階5bでAgentCore Runtimeへ移し、GPUのEC2とは同居させない | 判定の規則は無い。事例の回はすべてこの配置 |
| A2 | 2026-11-09 | GPUの2回目まで | 規則の追加 | エージェントのロールにAgentCore Memoryの読み書きを足す | 11-09より前の回の基準には、Memoryの書き込みが無い |
| A3 | 2026-11-14 | すべての回 | 明確化 | 無いADR-0009への参照をDESIGN §1に直し、起動の遅れの値を実測に直す | なし |

### A1 追記(2026-10-14): エージェントもAgentCore Runtimeへ移す

(決定日 2026-10-14)ADR-0003でGPUを使うことにしたのに合わせ、エージェントも段階5bでAgentCore Runtimeへ移す。推論サーバ(GPU)とエージェント(CPUのアプリ)は分けて置く。コミット f91ebbc。

### A2 追記(2026-11-09): エージェントのロールにMemoryの読み書きを足す

(決定日 2026-11-09)会話の履歴をAgentCore Memoryに書き、新しいセッションで読み戻すため、ロールに`CreateEvent`・`ListEvents`(このMemoryに限る)を足した(PR #39、`infra/agentcore_agent.tf`)。

### A3 追記(2026-11-14): 参照と値の明確化

(決定日 2026-11-14)決定の「ネットワークの詳細はADR-0009」は、書かなかったADRを指していたので、DESIGN §1の行に直した。妥当性への脅威の起動の遅れは、決めた時点では5〜7秒(DESIGN §7の12)と書いていたが、これはMCPのセッションIDを送り返さないときの値だった。新しいセッションの起動の実測(約1秒、§7の16)に直し、5〜7秒はその原因とともに並べた。
