# 業務ツールのカタログの調査メモ

調査日: 2026-10-05。業務ツールのカタログ(DESIGN §6の2b)の形を決める根拠として、公開されている指針と、
業務システムのMCPサーバの実例を集めた。ツールの数は調べた時点のもの。比較や順位づけはしていない。

## 指針: Anthropic「Writing effective tools for agents」

出典: https://www.anthropic.com/engineering/writing-tools-for-agents

- **APIをそのまま包まず、用途ごとにまとめる。** 例: `list_users`・`list_events`・`create_event`ではなく、
  空きを探して予定を入れる`schedule_event`。`get_customer_by_id`・`list_transactions`・`list_notes`ではなく
  `get_customer_context`。途中の結果でコンテキストを消費しないため
- **ツールは多ければよいわけではない。** 多すぎるツール・機能の重なるツールは、エージェントを効率的なやり方から
  逸らす。重要な業務に絞った少数のツールから始める
- **名前空間**: 共通の接頭辞でまとめると境界が分かりやすい(`asana_search`、`asana_projects_search`)。接頭辞か
  接尾辞かで評価の結果が変わったとある
- **返す量の制御**: 大きくなりうる結果には、ページ分け・範囲指定・絞り込み・切り詰めを、妥当な既定値つきで入れる。
  簡潔/詳細を選ぶ`response_format`引数(例では詳細206トークン、簡潔72トークン)。Claude Codeはツールの結果を
  既定で25,000トークンに制限している
- **意味のある値を返す**: `uuid`などの低レベルな識別子より名前を返す。識別子を意味のある語や0始まりの番号に
  置き換えるだけで、検索の精度が上がったとある

## 業務システムのMCPサーバの実例

| サーバ | 提供 | ツール数 | まとめ方 | データ構造の説明 | 結果の量の制御 |
|---|---|---|---|---|---|
| [freee](https://github.com/freee/freee-mcp) | 公式 | 15(API 5＋管理 10) | HTTPの操作ごと(`freee_api_get`/`post`/`put`/`patch`/`delete`)。522の操作をこの5つで呼ぶ | `freee_api_list_paths`(使えるエンドポイントの一覧) | 記載なし |
| [Salesforce Hosted MCP(sobject-all)](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/sobject-all.html) | 公式 | 9 | 汎用のデータ操作(`soqlQuery`・`find`・`createSobjectRecord`等)。読み取り専用などの別サーバもある | `getObjectSchema`(引数なしで一覧、名前を渡すと必須項目・選択肢・型) | 検索・最近のレコードは最大2,000件 |
| [Dynamics 365 ERP MCP](https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/dev-itpro/copilot/copilot-mcp) | 公式 | 22(データ 7・画面操作 13・業務処理 2) | 3種類: データのCRUD、画面の操作(人と同じように画面を開いて値を入れる)、業務処理の呼び出し | `data_find_entity_type`→`data_get_entity_metadata`の2段 | 記載なし。見えるツール・データは権限で絞られる |
| 同上の旧版(static) | 公式 | 13 | 業務の機能ごと(特定の業務処理を1つずつ) | — | — |
| [NetSuite MCP Standard Tools](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0902023508.html) | 公式 | 14 | 汎用のレコード操作＋レポート＋保存済み検索＋SQL(読み取りのみ) | `ns_getRecordTypeMetadata`・`ns_getSuiteQLMetadata` | 記載なし。見えるツールは権限で絞られる |
| [kintone](https://github.com/kintone/mcp-server) | 公式 | 26 | 操作×対象ごと(アプリ設定・レコード・スペース) | `kintone-get-form-fields` | 記載なし |
| マネーフォワード クラウド会計 | 公式(β) | 約21(第三者の記事より) | 操作×データの種類ごと(`getJournals`・`postJournals`・`getAccounts`等)、レポートは種類ごとに別ツール | — | 記載なし |
| [ServiceNow(echelon-ai-labs)](https://github.com/echelon-ai-labs/servicenow-mcp) | コミュニティ | 60以上 | 操作×データの種類ごと(`create_incident`・`resolve_incident`等) | — | 記載なし。役割ごとのツールの組(`MCP_TOOL_PACKAGE`)で、出すツールを絞れる |
| [odata_mcp_go](https://github.com/oisee/odata_mcp_go)(SAP等のOData) | コミュニティ | データの種類×最大7 / 1 | 通常はデータの種類ごとに自動生成(`filter_X`・`get_X`・`create_X`…)。汎用モードでは`odata`1つ | (汎用モードでは引数で指定) | 既定100件・5MB |
| [cap-mcp-bridge](https://github.com/SanoberRehman/cap-mcp-bridge)(SAP CAP/OData v4) | コミュニティ | 9 | 汎用のデータ操作。データの種類ごとには作らない | `list_entity_sets`・`describe_entity` | 未確認 |

SAPは、S/4HANAの業務データ向けの出来合いのMCPサーバを出しておらず、Integration Suiteで既存のAPIから
MCPサーバを作る形をとっているとされる(第三者の記事による。未確認)。

## 分かったこと

- **公式のサーバは、少数の汎用ツールとデータ構造の説明ツールを組み合わせた形が多い**(freee・Salesforce・
  Dynamics 365・NetSuite)。指針の「用途ごとにまとめる」に近いのはDynamics 365の旧版(業務の機能ごとの13個)で、
  これは汎用の新版に置き換えられた(旧版は2026-10-01に提供終了の予定と書かれている)
- **汎用型では、データ構造を調べる往復が先に入る。** Dynamics 365は種類を探す→構造を取る→データを引くの3段、
  Salesforceも構造を取ってからクエリを組み立てる
- **データの種類ごとに作ると、数が大きく膨らむ。** odata_mcp_goではSAPのサービス1つで485ツール・
  約37,000トークンになり、LLMが「使えるAPIが無い」と答えたとある。汎用モード(1ツール・約900トークン)を足した理由
- **ツールを絞る仕組みが実在する。** ServiceNowの役割ごとのツールの組、odata_mcp_goのデータの種類・操作の絞り込み、
  NetSuite・Dynamics 365の権限による表示の絞り込み
- **結果の量の制御を書いているのは少ない。** 明記していたのはSalesforce(最大2,000件)とodata_mcp_go(既定100件)だけ
