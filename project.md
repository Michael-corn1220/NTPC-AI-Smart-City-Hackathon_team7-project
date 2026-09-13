# 新北市法制局 AI 輔助訴願審理系統 — 專案指引

## 專案定位
輔助工具，非自動裁決系統。所有 AI 產出（分類、法規建議、多元視角分析、決定書草稿）皆須經承辦人員檢視、修改、核定後才是正式文書。

## 部署環境
- **雲端**：AWS，區域固定 `us-west-2`（黑客松規範指定 us-east-1／us-west-2 兩區之一）。
- **帳號**：Workshop Studio 臨時帳號（`ASIA` 開頭臨時憑證，會過期）。
- **資源前綴**：所有資源以 `ntpc-appeals` 為命名前綴，方便辨識與清理。

## 技術選型（已對齊支援服務清單）
| 分層 | 服務 |
|---|---|
| 前端 | React.js + Tailwind CSS |
| 身份驗證 | Amazon Cognito（單一角色示意） |
| API | Amazon API Gateway |
| 運算 | AWS Lambda（Python 3.11） |
| 編排 | AWS Step Functions（含 Bedrock ≤1 RPS 節流） |
| 去識別化 | Amazon Comprehend（PII） |
| 生成式 AI | Amazon Bedrock — **us.anthropic.claude-sonnet-4-5-20250929-v1:0**（inference profile；帳號無 Claude 3.5 Sonnet，且新版 Claude 不支援 on-demand 直呼，必須用 us. 前綴 inference profile），受 Guardrails 保護 |
| 向量嵌入 | **amazon.titan-embed-text-v2:0**（1024 維，on-demand 可直呼） |
| 向量檢索 | Bedrock Knowledge Bases + OpenSearch Serverless |
| 重排 | SageMaker JumpStart Cross-Encoder rerank 端點 |
| 儲存 | Amazon S3、Amazon DynamoDB（On-Demand） |
| 加密 | AWS KMS |
| 監控 | Amazon CloudWatch |

## 黑客松合規強制事項（優先於一般功能規格）
1. **資料**：競賽帳號僅得使用合成／已徹底去識別化資料，禁止匯入真實個資、財務資訊、受管制資料。
2. **S3**：一律開啟 Block Public Access，禁止公開 bucket。
3. **法規資料來源**：法規/函釋/裁罰基準/決定書等 KB 來源文件僅得存放於指定 S3 bucket，經 Knowledge Bases ingestion 後方可檢索；禁止即時上網查法規或憑模型記憶生成法條。
4. **Bedrock 速率**：對 Bedrock 請求須 ≤1 RPS/TPS，以 Step Functions 併發控制或 SQS 佇列節流。
5. **模型存取**：僅申請專案相關模型，定期檢視撤銷未用模型。
6. **執行個體**：僅啟動必要數量，不做大規模訓練；Cross-Encoder 用既有預訓練模型直接部署推論。
7. **機密管理**：程式碼上公開儲存庫前確認無憑證；用 .gitignore + 環境變數/Parameter Store 管理。
8. **/.kiro 保留**：上傳公開儲存庫時須保留 /.kiro（specs/hooks/steering），且不得加入 .gitignore。

## 開發注意事項（本機環境）
- 本機終端機互動式輸出有回顯異常，凡需觀察執行結果的腳本，一律寫入檔案後以檔案讀取確認，勿依賴終端 stdout。
- 所有 boto3 呼叫使用 `Config(retries={"max_attempts":3,"mode":"standard"})` 並在批次呼叫間加入延遲，避免觸發 AWS rate limit。

## SageMaker rerank 端點選型（依支援清單）
- 支援清單 SageMaker AI 工作表 `endpoint/ml.*` 為即時推論可用執行個體。
- 選用 **ml.g4dn.xlarge**（GPU T4，限額 2，只開 1 台）部署 Cross-Encoder rerank；CPU 備選 ml.m5.xlarge(限額4)/ml.c5.xlarge(限額4)。
- `transform-job/ml.*` 全部限額 0（不可用批次轉換），故 rerank 用即時 endpoint。
- 僅部署推論端點，不做訓練（符合規範）。
