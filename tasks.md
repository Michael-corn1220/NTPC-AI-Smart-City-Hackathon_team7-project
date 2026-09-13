# 實作任務 — 基礎資源建置

- [x] 1. 能力探測與模型可用性驗證
  - 確認憑證權限、Bedrock 可用模型與 inference profile 需求
  - _需求: 3_

- [ ] 2. 建立 KMS 金鑰（alias/ntpc-appeals）
  - _需求: 6.1_

- [ ] 3. 建立 S3 buckets（kb-source + artifacts），Block Public Access + SSE-KMS + 目錄前綴
  - _需求: 1_

- [ ] 4. 建立 DynamoDB 三表（On-Demand + KMS）
  - _需求: 2_

- [ ] 5. 建立 Bedrock Guardrails
  - _需求: 3.3_

- [ ] 6. 建立 OpenSearch Serverless collection + 安全/存取政策
  - _需求: 3.4_

- [ ] 7. 建立 IAM 角色（KB 服務角色、Lambda 執行角色）
  - _需求: 6.2_

- [ ] 8. 建立 Bedrock Knowledge Base + Custom Chunking Lambda + data source
  - _需求: 3.4, 3.5_

- [ ] 9. 部署 SageMaker Cross-Encoder rerank 端點
  - _需求: 4_

- [ ] 10. 建立 Cognito User Pool
  - _需求: 5.1_

- [ ] 11. 建立 Lambda + API Gateway + Step Functions（含 ≤1 RPS 節流）
  - _需求: 5.2, 5.3_

- [ ] 12. 整體驗證與資源清單輸出
  - _需求: 全部_
