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

- MCPサーバ: AgentCore RuntimeのVPCモード(ネットワークの詳細はADR-0009)。状態を持たない作り(`stateless_http`)にする。
  認証はSigV4にする。手元のクレデンシャルで済み、Cognito(ユーザー管理)が要らない。
- 段階5まではエージェントを手元に置く。MCPサーバの移行と分けて確かめるため。
- エージェント(A1): 段階5bでAgentCore Runtimeへ移す。ロールは、自分のイメージの取得とMCPサーバのRuntimeの呼び出しだけで、RDSの権限は持たない。

## 影響(Consequences)

- 得るもの: 基盤のspan(`AgentCore.Runtime.Invoke`)が、自作のspanと同じトレースに入る(段階5・5bで確認)。
- 失うもの:
  - セッションごとに専用のmicroVMを起動する作りなので、新しいセッションの最初の呼び出しと、使わない時間(初期設定で15分)の後の呼び出しには、起動の遅れが入る。常駐の形なら入らない。
  - イメージはARM64で作る必要があり、ビルドとpushに1つ約10分かかる。
  - Runtimeを消した後、ネットワークインターフェースが最大8時間残り、VPCをすぐには消せない(DESIGN §8)。
- 残る穴: 専用のインスタンスで動かす種類(Instances)は調べていない。

### 測定に混ざるもの

- 起動の遅れ(5〜7秒、DESIGN §7の12)が、ほかの遅れより大きく出ることがある。事例で測るときは、セッションIDを固定し、
  測る前に1周流して温める。基盤の分は、`AgentCore.Runtime.Invoke`と`invoke_agent`の差で分ける。

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

| 番号 | 日付 | 種類 | 中身 |
| --- | --- | --- | --- |
| A1 | 2026-10-14 | 変更 | エージェントも段階5bでAgentCore Runtimeへ移す。GPUのEC2とは同居させない(ADR-0003でGPUを使うことにしたのに合わせた。コミット f91ebbc) |
