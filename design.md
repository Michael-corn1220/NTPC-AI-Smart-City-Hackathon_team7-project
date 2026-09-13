# 設計文件 — 新北市法制局 AI 輔助訴願審理系統（基礎資源建置）

## 概觀
以 boto3 腳本（idempotent，可重複執行）在 AWS us-west-2 建立系統所需雲端資源。所有腳本置於 `infra/`，結果寫入檔案供稽核。全程序列化呼叫並節流，避免觸發 rate limit。

## 架構

```
使用者 → Cognito 驗證 → API Gateway → Lambda → Step Functions 狀態機
                                                     │
   ①Comprehend 去識別化 → ②Claude 分類/擷取 → ③分流 →
   ④Hybrid Search（OpenSearch + Titan 向量, KB）→
   ⑤SageMaker Rerank → ⑥Claude 草稿生成（Guardrails 保護）
                                                     │
                          S3（法規來源/卷證/草稿） + DynamoDB（3 表）
```

## 元件與命名（前綴 ntpc-appeals）

| 資源 | 名稱 |
|---|---|
| KMS 金鑰別名 | `alias/ntpc-appeals` |
| S3 法規來源 | `ntpc-appeals-kb-source-<accountid>` |
| S3 卷證/草稿 | `ntpc-appeals-artifacts-<accountid>` |
| DynamoDB | `ntpc-appeals-cases` / `ntpc-appeals-drafts` / `ntpc-appeals-audit-logs` |
| Guardrail | `ntpc-appeals-guardrail` |
| OpenSearch Serverless | collection `ntpc-appeals-kb` |
| Knowledge Base | `ntpc-appeals-kb` |
| SageMaker 端點 | `ntpc-appeals-rerank` |
| Cognito | `ntpc-appeals-users` |
| API Gateway | `ntpc-appeals-api` |
| Step Functions | `ntpc-appeals-pipeline` |

## 關鍵決策
- **生成模型**：`us.anthropic.claude-sonnet-4-5-20250929-v1:0`（inference profile；新版 Claude 不支援 on-demand 直呼）。
- **embedding**：`amazon.titan-embed-text-v2:0`（1024 維）。
- **節流**：Step Functions 呼叫 Bedrock 的 Lambda 以 reserved concurrency=1 + Lambda 內 sleep，確保 ≤1 RPS。
- **DynamoDB**：On-Demand，SSE-KMS。
- **S3**：Block Public Access 全開，SSE-KMS，bucket policy 僅允許服務角色。

## 執行順序（依相依性）
1. KMS 金鑰（其他資源加密依賴）
2. S3 buckets
3. DynamoDB 表
4. Bedrock Guardrails
5. OpenSearch Serverless collection（+ 安全/存取政策）
6. IAM 角色（KB 服務角色、Lambda 執行角色）
7. Bedrock Knowledge Base + Custom Chunking Lambda + data source
8. SageMaker rerank 端點
9. Cognito User Pool
10. Lambda + API Gateway + Step Functions

## 錯誤處理
- 每個建立腳本先檢查資源是否已存在（list/describe），存在則跳過或沿用，達成 idempotent。
- 所有 boto3 client 使用 `Config(retries={"max_attempts":3,"mode":"standard"})`。
- 結果與資源 ID 寫入 `infra/state.json`，供後續步驟引用與最終清單輸出。
