# 需求文件 — 新北市法制局 AI 輔助訴願審理系統

## 簡介
本系統協助新北市政府法制局處理訴願案件（洗錢防制法、廢棄物清理法、空氣污染防制法等大宗案件），提供案件結構化擷取、法規與函釋推薦、相似案例檢索、中立多元視角分析、決定書草稿生成。系統為輔助工具，非自動裁決系統。

本需求文件聚焦於「雲端基礎資源建置」範圍。

## 需求

### 需求 1：法規資料來源儲存（S3）
**使用者故事：** 身為資料維運人員，我要一個受控的 S3 bucket 存放法規來源文件，以確保 AI 只檢索經核可的法規版本。

#### 驗收準則
1. WHEN 建立法規來源 bucket THEN 系統 SHALL 開啟 Block Public Access（四項全開）。
2. WHEN 建立法規來源 bucket THEN 系統 SHALL 建立 `laws/`、`interpretations/`、`decisions/`、`penalty_tables/` 四個目錄前綴。
3. WHEN 建立任何 bucket THEN 系統 SHALL 啟用預設加密（SSE-KMS）。

### 需求 2：案件資料儲存（DynamoDB）
**使用者故事：** 身為系統，我要持久化案件、草稿與稽核紀錄。

#### 驗收準則
1. 系統 SHALL 建立 `AppealsCases`（PK: case_id）。
2. 系統 SHALL 建立 `DraftDecisions`（PK: draft_id, SK: case_id）。
3. 系統 SHALL 建立 `SystemAuditLogs`（PK: log_id, SK: timestamp[Number]）。
4. 所有表 SHALL 使用 On-Demand 計費模式並啟用 KMS 加密。

### 需求 3：生成式 AI 與向量檢索（Bedrock）
**使用者故事：** 身為承辦人員，我要 AI 依據受控法規產生草稿，且不得引用清單外法條。

#### 驗收準則
1. 系統 SHALL 使用 inference profile `us.anthropic.claude-sonnet-4-5-20250929-v1:0` 作為生成模型。
2. 系統 SHALL 使用 `amazon.titan-embed-text-v2:0` 作為 embedding 模型。
3. 系統 SHALL 建立 Bedrock Guardrails（Contextual Grounding、Denied Topics、PII 過濾、Prompt Attack、中立視角內容政策）。
4. 系統 SHALL 建立 OpenSearch Serverless collection 與 Bedrock Knowledge Base，資料源指向需求 1 的 S3 bucket。
5. Knowledge Base SHALL 使用 CUSTOM chunking，掛 Lambda 依 Markdown 標題切塊。

### 需求 4：相似案例重排（SageMaker）
#### 驗收準則
1. 系統 SHALL 部署一個 Cross-Encoder rerank 推論端點，執行個體類型須落於支援清單範圍。
2. 系統 SHALL 僅啟動必要數量端點，不做訓練。

### 需求 5：應用層（Serverless）
#### 驗收準則
1. 系統 SHALL 建立 Cognito User Pool（單一角色示意）。
2. 系統 SHALL 建立 API Gateway、Lambda、Step Functions 實作規格 5.3 節端點與六階段流程。
3. WHEN 呼叫 Bedrock THEN 系統 SHALL 以節流機制確保 ≤1 RPS/TPS。

### 需求 6：安全與合規
#### 驗收準則
1. 系統 SHALL 以 KMS 金鑰進行靜態加密。
2. 系統 SHALL 遵循最小權限 IAM。
3. 競賽帳號 SHALL 僅使用合成／去識別化資料。
4. 所有資源 SHALL 建立於 us-west-2。
