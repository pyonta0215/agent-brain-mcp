---
id: playbook-write-back-criteria
type: playbook
status: active
title: Brain へ書き戻すときの基準
domain: agents
confidence: medium
---
## いつ何を残すか

| 残すもの | ツール | 基準 |
|---|---|---|
| 設計判断 | `brain_record_decision` | 後から「なぜこうしたか」を聞かれそうなもの。採らなかった案と理由を必ず書く |
| 学び | `brain_record_memory` | 次の作業者が同じ調査をしなくて済むもの。候補として保存し、昇格は根拠があるときだけ |
| 作業の記録 | `brain_log_action` | 作業の終わりに1回。失敗も残し、`next` で次の作業者へ引き継ぐ |

## 残さないもの

- API キー・トークン・パスワードなどの秘密情報、個人情報
- 生のログや差分（要点と参照先だけを書く）
- コードやコミット履歴を見れば分かること
