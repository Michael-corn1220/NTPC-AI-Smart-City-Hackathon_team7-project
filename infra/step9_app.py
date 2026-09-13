# -*- coding: utf-8 -*-
"""部署應用層：pipeline Lambda + api Lambda + Step Functions 狀態機 + API Gateway。
pipeline Lambda 設 reserved concurrency=1（Bedrock <=1 RPS 節流）。Idempotent。
結果寫入 step9_result.txt。
"""
import io
import json
import os
import time
import zipfile
from botocore.exceptions import ClientError
from common import client, slp, get_account_id, load_state, set_state, Logger, REGION, PREFIX

L = Logger("step9_result.txt")

PIPELINE_FN = f"{PREFIX}-pipeline"
API_FN = f"{PREFIX}-api"
SM_NAME = f"{PREFIX}-pipeline-sm"
API_NAME = f"{PREFIX}-api"
STAGE = "v1"
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lambda_src")
GEN_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def zip_dir(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, path))
    buf.seek(0)
    return buf.read()


def deploy_lambda(lam, name, src_subdir, env, role_arn, timeout=120, mem=512, reserved=None):
    code = zip_dir(os.path.join(SRC, src_subdir))
    exists = True
    try:
        lam.get_function(FunctionName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            exists = False
        else:
            raise
    if exists:
        lam.update_function_code(FunctionName=name, ZipFile=code)
        L.log(f"[OK] 更新 Lambda 程式碼 {name}")
        slp()
        # 等程式碼更新完成再改設定
        for _ in range(10):
            st = lam.get_function(FunctionName=name)["Configuration"].get("LastUpdateStatus")
            if st == "Successful":
                break
            time.sleep(3)
        lam.update_function_configuration(
            FunctionName=name, Environment={"Variables": env}, Timeout=timeout, MemorySize=mem)
        L.log(f"[OK] 更新 Lambda 設定 {name}")
        slp()
    else:
        last = None
        for i in range(6):
            try:
                lam.create_function(
                    FunctionName=name, Runtime="python3.12", Role=role_arn,
                    Handler="handler.handler", Code={"ZipFile": code},
                    Timeout=timeout, MemorySize=mem,
                    Environment={"Variables": env},
                    Tags={"project": PREFIX})
                L.log(f"[OK] 建立 Lambda {name}")
                last = None
                break
            except ClientError as ce:
                last = ce.response["Error"]["Code"]
                L.log(f"  create {name} 重試 {i+1}/6: {last}")
                time.sleep(10)
        if last:
            raise RuntimeError(f"create {name} failed: {last}")
        slp()
    if reserved is not None:
        try:
            lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=reserved)
            L.log(f"[OK] {name} reserved concurrency={reserved}（Bedrock 節流）")
        except ClientError as e:
            L.log(f"[WARN] put_function_concurrency {name}: {e.response['Error']['Code']}")
        slp()
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def build_state_machine_def(pipeline_arn):
    """六階段：去識別化(訴願書+答辯書)→擷取分類→分流→法規檢索→草稿生成→(結束)。
    每步呼叫 pipeline Lambda 的對應 stage。相似案例 rerank 內含於 retrieve 之後（簡化）。
    """
    def task(stage, payload_expr, result_path, next_state, end=False):
        s = {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"FunctionName": pipeline_arn,
                           "Payload": {"stage": stage, "payload.$": payload_expr}},
            "ResultSelector": {"result.$": "$.Payload.result"},
            "ResultPath": result_path,
            "Retry": [{"ErrorEquals": ["Lambda.TooManyRequestsException",
                                       "States.TaskFailed", "Lambda.ServiceException"],
                       "IntervalSeconds": 3, "MaxAttempts": 4, "BackoffRate": 2.0}],
        }
        if end:
            s["End"] = True
        else:
            s["Next"] = next_state
        return s

    return {
        "Comment": "NTPC 訴願六階段 pipeline（Bedrock <=1 RPS，透過 pipeline Lambda reserved concurrency=1）",
        "StartAt": "DeidentifyAppellant",
        "States": {
            # ① 去識別化（訴願書）
            "DeidentifyAppellant": {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": pipeline_arn,
                               "Payload": {"stage": "deidentify",
                                           "payload": {"text.$": "$.appellant_text"}}},
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.deid_appellant", "Next": "DeidentifyAgency",
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 3,
                           "MaxAttempts": 4, "BackoffRate": 2.0}],
            },
            # ① 去識別化（答辯書）
            "DeidentifyAgency": {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": pipeline_arn,
                               "Payload": {"stage": "deidentify",
                                           "payload": {"text.$": "$.agency_text"}}},
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.deid_agency", "Next": "Extract",
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 3,
                           "MaxAttempts": 4, "BackoffRate": 2.0}],
            },
            # ② 結構化擷取 + 分類 + 程序駁回判斷
            "Extract": {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": pipeline_arn,
                               "Payload": {"stage": "extract",
                                           "payload": {
                                               "appellant_text.$": "$.deid_appellant.result.deidentified_text",
                                               "agency_text.$": "$.deid_agency.result.deidentified_text"}}},
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.extract", "Next": "Retrieve",
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 3,
                           "MaxAttempts": 4, "BackoffRate": 2.0}],
            },
            # ④ 法規/案例檢索（KB Hybrid + rerank）
            "Retrieve": {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": pipeline_arn,
                               "Payload": {"stage": "retrieve",
                                           "payload": {"query.$": "$.deid_appellant.result.deidentified_text",
                                                       "category.$": "$.category",
                                                       "structured.$": "$.extract.result.structured"}}},
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.retrieve", "Next": "Draft",
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 3,
                           "MaxAttempts": 4, "BackoffRate": 2.0}],
            },
            # ⑥ 草稿生成
            "Draft": {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": pipeline_arn,
                               "Payload": {"stage": "draft",
                                           "payload": {
                                               "structured.$": "$.extract.result.structured",
                                               "regulation_matches.$": "$.retrieve.result.regulation_matches"}}},
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.draft", "Next": "Finalize",
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 3,
                           "MaxAttempts": 4, "BackoffRate": 2.0}],
            },
            # 整合寫回 DynamoDB（供 result 端點查詢）
            "Finalize": {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": pipeline_arn,
                               "Payload": {"stage": "finalize",
                                           "payload": {
                                               "case_id.$": "$.case_id",
                                               "structured.$": "$.extract.result.structured",
                                               "regulation_matches.$": "$.retrieve.result.regulation_matches",
                                               "regulation_list.$": "$.retrieve.result.regulation_list",
                                               "similar_cases.$": "$.retrieve.result.similar_cases",
                                               "procedural_skip.$": "$.retrieve.result.skipped_reason",
                                               "draft.$": "$.draft.result.draft"}}},
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.finalize", "End": True,
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 3,
                           "MaxAttempts": 4, "BackoffRate": 2.0}],
            },
        },
    }


def main():
    acc = get_account_id()
    st = load_state()
    lam = client("lambda")
    role_arn = st["lambda_role_arn"]

    # ---- 1. pipeline Lambda（reserved concurrency=1 節流 Bedrock）----
    pipeline_env = {
        "GEN_MODEL_ID": GEN_MODEL,
        "GUARDRAIL_ID": st.get("guardrail_id", ""),
        "GUARDRAIL_VERSION": st.get("guardrail_version", "1"),
        "KB_ID": st.get("kb_id", ""),
        "RERANK_ENDPOINT": st.get("rerank_endpoint_name", ""),
        "CASES_TABLE": f"{PREFIX}-cases",
        "DRAFTS_TABLE": f"{PREFIX}-drafts",
    }
    # concurrency=3：允許六階段 pipeline 與即時操作(perspective/revise/chat)並存；
    # Bedrock <=1 RPS 主要靠 pipeline 內每次呼叫 _throttle() sleep 1.1s 保障，非靠 concurrency=1。
    pipeline_arn = deploy_lambda(lam, PIPELINE_FN, "pipeline", pipeline_env, role_arn,
                                 timeout=180, mem=512, reserved=3)
    set_state("pipeline_lambda_arn", pipeline_arn)

    # ---- 2. Step Functions 狀態機 ----
    sfn = client("stepfunctions")
    sm_def = build_state_machine_def(pipeline_arn)
    existing_sm = None
    try:
        for m in sfn.list_state_machines(maxResults=1000).get("stateMachines", []):
            if m["name"] == SM_NAME:
                existing_sm = m["stateMachineArn"]
    except ClientError as e:
        L.log(f"[WARN] list_state_machines: {e}")
    slp()
    if existing_sm:
        sfn.update_state_machine(stateMachineArn=existing_sm,
                                 definition=json.dumps(sm_def), roleArn=st["sfn_role_arn"])
        sm_arn = existing_sm
        L.log(f"[OK] 更新 Step Functions {SM_NAME}")
    else:
        last = None
        for i in range(6):
            try:
                r = sfn.create_state_machine(
                    name=SM_NAME, definition=json.dumps(sm_def),
                    roleArn=st["sfn_role_arn"], type="STANDARD",
                    tags=[{"key": "project", "value": PREFIX}])
                sm_arn = r["stateMachineArn"]
                L.log(f"[OK] 建立 Step Functions {SM_NAME}")
                last = None
                break
            except ClientError as ce:
                last = ce.response["Error"]["Code"]
                L.log(f"  create SM 重試 {i+1}/6: {last}")
                time.sleep(10)
        if last:
            raise RuntimeError(f"create state machine failed: {last}")
    set_state("state_machine_arn", sm_arn)
    slp()

    # ---- 3. api Lambda（回填 STATE_MACHINE_ARN）----
    api_env = {
        "CASES_TABLE": f"{PREFIX}-cases",
        "DRAFTS_TABLE": f"{PREFIX}-drafts",
        "AUDIT_TABLE": f"{PREFIX}-audit-logs",
        "STATE_MACHINE_ARN": sm_arn,
        "PIPELINE_FN": PIPELINE_FN,
        "KB_SOURCE_BUCKET": "legal-dataset-hackathon-191149991440",
    }
    api_arn = deploy_lambda(lam, API_FN, "api", api_env, role_arn, timeout=120, mem=256)
    set_state("api_lambda_arn", api_arn)

    # ---- 4. API Gateway REST（proxy 整合 api Lambda）----
    apigw = client("apigateway")
    api_id = None
    try:
        for a in apigw.get_rest_apis(limit=500).get("items", []):
            if a["name"] == API_NAME:
                api_id = a["id"]
    except ClientError as e:
        L.log(f"[WARN] get_rest_apis: {e}")
    slp()

    if not api_id:
        r = apigw.create_rest_api(name=API_NAME, description="NTPC appeals API",
                                  endpointConfiguration={"types": ["REGIONAL"]},
                                  tags={"project": PREFIX})
        api_id = r["id"]
        L.log(f"[OK] 建立 REST API {API_NAME} -> {api_id}")
        slp()
    else:
        L.log(f"[SKIP] REST API 已存在 {API_NAME} -> {api_id}")

    set_state("api_id", api_id)

    # 取得 root resource
    resources = apigw.get_resources(restApiId=api_id, limit=500)["items"]
    root_id = next(r["id"] for r in resources if r["path"] == "/")
    existing_paths = {r["path"]: r["id"] for r in resources}
    slp()

    # 建立 {proxy+} 代理資源（若尚無）
    proxy_id = existing_paths.get("/{proxy+}")
    if not proxy_id:
        rp = apigw.create_resource(restApiId=api_id, parentId=root_id, pathPart="{proxy+}")
        proxy_id = rp["id"]
        L.log("[OK] 建立 {proxy+} 資源")
        slp()

    lambda_uri = (f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
                  f"{api_arn}/invocations")

    # ANY 方法 + AWS_PROXY 整合（proxy 資源 + root 資源）
    for res_id in (proxy_id, root_id):
        try:
            apigw.put_method(restApiId=api_id, resourceId=res_id, httpMethod="ANY",
                             authorizationType="NONE", apiKeyRequired=False)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":
                L.log(f"[WARN] put_method: {e.response['Error']['Code']}")
        slp(0.3)
        try:
            apigw.put_integration(restApiId=api_id, resourceId=res_id, httpMethod="ANY",
                                  type="AWS_PROXY", integrationHttpMethod="POST",
                                  uri=lambda_uri)
        except ClientError as e:
            L.log(f"[WARN] put_integration: {e.response['Error']['Code']}")
        slp(0.3)
    L.log("[OK] 設定 ANY + AWS_PROXY 整合")

    # 允許 API Gateway 呼叫 api Lambda
    try:
        lam.add_permission(FunctionName=API_FN, StatementId="apigw-invoke",
                           Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
                           SourceArn=f"arn:aws:execute-api:{REGION}:{acc}:{api_id}/*/*/*")
        L.log("[OK] 允許 API Gateway 呼叫 api Lambda")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            L.log("[SKIP] apigw invoke 權限已存在")
        else:
            L.log(f"[WARN] add_permission: {e.response['Error']['Code']}")
    slp()

    # 部署到 stage
    apigw.create_deployment(restApiId=api_id, stageName=STAGE,
                            description="deploy")
    api_url = f"https://{api_id}.execute-api.{REGION}.amazonaws.com/{STAGE}"
    L.log(f"[OK] 部署 API 到 stage {STAGE}")
    L.log(f"  API URL: {api_url}")
    set_state("api_url", api_url)

    L.log("STEP9 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
