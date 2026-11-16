# ADR-0003: 段階5b以降のエンジンは、vLLM 0.31.0とQwen3-8B bf16にする

- Status: Accepted
- Date: 2026-10-31

## 結論(Summary)

- エンジンはvLLM 0.31.0、モデルはQwen3-8B bf16(GPUはADR-0002のg6.xlarge・L4 1台)。thinkingはオフ。
- 選んだ基準は、エンジンの中で起きる遅れ(事例8/9・10・M1・12)を見る手段があること。
- リクエストごとの内訳は応答の本文から取り、エンジンのspanは補助にする。
- エンジンを比べ直すことはしない。

## 背景と問い(Context and Problem Statement)

[toolcall-serving-bench](https://github.com/yuya0408/toolcall-serving-bench)(L4 1台・Qwen3-8B)で精度の足切りを通ったのは、vLLM bf16・FP8とTensorRT-LLM bf16・FP8の4構成だった。
目標の並行数が2以下では4構成が同じ台数で並び、ベンチはどれか1つに決めていない。
このプロジェクトは質問を1つずつ流すので、並行数は1〜2になり、ベンチの結論からはエンジンが決まらない。

問い: 4構成のうち、どれで事例を再現するか。

## 判断の基準(Decision Drivers)

- 事例のうち、エンジンの中で起きるもの(8/9・10・M1・12)を、外から切り分けられること。
  具体的には、待ち行列・プリフィル・デコードの時間と、キャッシュに当たったトークン数がリクエストごとに取れること。
- エージェントのトレースと、エンジンの記録がつながること(`traceparent`を引き継ぐ)。

## 検討した案(Considered Options)

| 案 | 採否 | 理由 |
| --- | --- | --- |
| vLLM bf16 | 採用 | HTTPの`traceparent`を親にして`llm_request` spanを作り、待ち行列・プリフィル・デコードの時間を付ける。応答の本文にも待ち行列の時間とキャッシュに当たったトークン数が入る(v0.31.0のソースで確認) |
| TensorRT-LLM(bf16・FP8) | 却下 | トレースを送る設定(`otlp_traces_endpoint`)は、1.2.1でも試作(`status: prototype`)の扱いで、親の引き継ぎを確かめていない。並行数4以上での台数の利点は、並行数1〜2では効かない |
| vLLMのFP8 | 却下 | 並行数1〜2では台数の利点が無い。vLLMのFP8はL4ではW8A16で計算する、という注意書きが増える。文脈の長さがbf16で載らなければ見直す |
| SGLang・AWQ | 却下 | ベンチで精度の非劣化を示せなかった |

## 決定(Decision Outcome)

- vLLM 0.31.0(公式イメージ`vllm/vllm-openai:v0.31.0`)、Qwen3-8B bf16。版と起動の手順はベンチで固定済みのものを使う。
- 起動フラグ: `--enable-per-request-metrics`・`--enable-prompt-tokens-details`(応答に内訳が入る)、`--otlp-traces-endpoint`(エンジンのspan)、
  `--max-model-len 16384`(KVは22,976トークンで載った。DESIGN §8)。
- 応答の内訳をspanに記録する方法はDESIGN §6の5b、エンジンのspanの中継はDESIGN §1「エンジンのspanの中継」(A1)。

## 影響(Consequences)

- 得るもの: `chat`(エージェント)→`llm_request`(vLLM)が同じトレースに入り、待ち行列などの時間が属性で見える(2026-11-01に確認)。
- 失うもの: llama.cppの`timings`とは項目の名前が違うので、記録する側を分けた。
- 残る穴: KVの大きさが`torch.compile`のキャッシュの有無で変わる(22,976と約30,240)。事例の間でサーバを起動し直すときは、コンテナを作り直してそろえる(DESIGN §8)。

## 参考(More Information)

- DESIGN §1「段階5b以降のエンジン」、§6の5b、§8の「仮のGPUサーバ」「出口の無いVPCのGPUサーバ」
- toolcall-serving-benchの[README](https://github.com/yuya0408/toolcall-serving-bench#readme)(足切りの結論)・[ADR-0006](https://github.com/yuya0408/toolcall-serving-bench/blob/main/docs/adr/0006-pin-engine-versions.md)(版の固定)
- TensorRT-LLMの設定の安定度: [tests/unittest/api_stability/references/llm.yaml(v1.2.1)](https://github.com/NVIDIA/TensorRT-LLM/blob/v1.2.1/tests/unittest/api_stability/references/llm.yaml)
  の`otlp_traces_endpoint`(2026-11-03に確認。mainでも`prototype`)
- コミット 03a8deb(2026-10-31、決定の記録)。
- 2026-11-03に、DESIGN §1から移した。

## 追記(Amendments)

種類: **明確化**=書き方だけを直した/**規則の追加**=その規則で読む結果を見る前に決めた/**結果の後の補完**=その規則で読む結果を見た後に決めた(理由を本文に書く)/**適用の記録**=決めたとおりに読んだ結果。結果を見た後に決めたことそのものを変えるときは、新しいADRを立ててこのADRをSupersededにする。

| 番号 | 日付 | 見ていた結果 | 種類 | 中身 | 判定への影響 |
| --- | --- | --- | --- | --- | --- |
| A1 | 2026-11-14 | すべての回 | 明確化 | 無いADR-0006への参照をDESIGN §1に直す | なし |

### A1 追記(2026-11-14): 参照の明確化

(決定日 2026-11-14)決定の「エンジンのspanの中継はADR-0006」は、書かなかったADRを指していたので、DESIGN §1の行に直した。参考の`toolcall-serving-bench`のADR-0006(版の固定)は別のリポジトリのもの。
