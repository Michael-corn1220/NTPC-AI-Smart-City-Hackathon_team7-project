# -*- coding: utf-8 -*-
"""API handler Lambda：實作規格 5.3 節 REST 端點。經 API Gateway proxy 整合。
案件/草稿主鍵由後端產生（case_id/draft_id）。觸發分析時啟動 Step Functions。
"""
import json
import os
import time
import uuid
import boto3
from decimal import Decimal

REGION = os.environ.get("AWS_REGION", "us-west-2")
CASES_TABLE = os.environ["CASES_TABLE"]
DRAFTS_TABLE = os.environ["DRAFTS_TABLE"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN")
PIPELINE_FN = os.environ.get("PIPELINE_FN")
KB_SOURCE_BUCKET = os.environ.get("KB_SOURCE_BUCKET", "legal-dataset-hackathon-191149991440")

ddb = boto3.resource("dynamodb", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


def invoke_pipeline(payload, retries=6):
    """同步呼叫 pipeline Lambda。pipeline reserved concurrency=1，若被節流則等待重試，
    以嚴守 Bedrock <=1 RPS 的同時讓即時操作（revise/perspective）最終成功。"""
    import time as _t
    last = None
    for i in range(retries):
        try:
            resp = lam.invoke(
                FunctionName=PIPELINE_FN, InvocationType="RequestResponse",
                Payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return json.loads(resp["Payload"].read())
        except Exception as e:  # noqa
            last = e
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
            if "TooManyRequests" in str(e) or code == "TooManyRequestsException":
                _t.sleep(3 + i * 2)
                continue
            raise
    raise last


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _audit(action, case_id, ms=0):
    try:
        ddb.Table(AUDIT_TABLE).put_item(Item={
            "log_id": _new_id("log"), "timestamp": Decimal(str(int(time.time() * 1000))),
            "action_type": action, "case_id": case_id or "-",
            "execution_duration_ms": Decimal(str(ms)),
        })
    except Exception:  # noqa
        pass


def create_case(body):
    case_id = _new_id("c")
    item = {
        "case_id": case_id,
        "case_no": body.get("case_no", ""),
        "category": body.get("category", ""),
        "appellant_text": body.get("appellant_text", ""),
        "agency_text": body.get("agency_text", ""),
        "status": "created",
        "created_at": int(time.time()),
    }
    ddb.Table(CASES_TABLE).put_item(Item=item)
    _audit("create_case", case_id)
    return _resp(201, {"case_id": case_id, "status": "created"})


def analyze(case_id):
    if not STATE_MACHINE_ARN:
        return _resp(500, {"error": "state machine not configured"})
    r = ddb.Table(CASES_TABLE).get_item(Key={"case_id": case_id})
    if "Item" not in r:
        return _resp(404, {"error": "case not found"})
    item = r["Item"]
    exe = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=f"{case_id}-{int(time.time())}",
        input=json.dumps({
            "case_id": case_id,
            "category": item.get("category", ""),
            "appellant_text": item.get("appellant_text", ""),
            "agency_text": item.get("agency_text", ""),
        }, ensure_ascii=False),
    )
    ddb.Table(CASES_TABLE).update_item(
        Key={"case_id": case_id},
        UpdateExpression="SET #s=:s, execution_arn=:e",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "analyzing", ":e": exe["executionArn"]},
    )
    _audit("analyze", case_id)
    return _resp(202, {"job_id": exe["executionArn"], "status": "analyzing"})


def analyze_status(case_id):
    r = ddb.Table(CASES_TABLE).get_item(Key={"case_id": case_id})
    if "Item" not in r:
        return _resp(404, {"error": "case not found"})
    arn = r["Item"].get("execution_arn")
    if not arn:
        return _resp(200, {"status": r["Item"].get("status", "unknown")})
    d = sfn.describe_execution(executionArn=arn)
    return _resp(200, {"status": d["status"], "case_status": r["Item"].get("status")})


def get_result(case_id):
    r = ddb.Table(CASES_TABLE).get_item(Key={"case_id": case_id})
    if "Item" not in r:
        return _resp(404, {"error": "case not found"})
    item = r["Item"]
    out = {
        "case_id": case_id,
        "case_no": item.get("case_no", ""),
        "category": item.get("category", ""),
        "status": item.get("status", ""),
        "draft_id": item.get("draft_id", ""),
    }
    # 解析 pipeline 寫回的完整結果
    if item.get("result_json"):
        try:
            out["result"] = json.loads(item["result_json"])
        except Exception:  # noqa
            out["result"] = None
    return _resp(200, out)


def perspective_analysis(case_id):
    """中立多元視角分析：同步呼叫 pipeline Lambda 的 perspective 階段。不寫入 DraftDecisions。"""
    if not PIPELINE_FN:
        return _resp(500, {"error": "pipeline function not configured"})
    r = ddb.Table(CASES_TABLE).get_item(Key={"case_id": case_id})
    if "Item" not in r:
        return _resp(404, {"error": "case not found"})
    item = r["Item"]
    # 取結構化擷取的雙方論點（若已完成分析）；否則用原文
    appellant_points, agency_points = [], []
    if item.get("result_json"):
        try:
            structured = json.loads(item["result_json"]).get("structured", {})
            appellant_points = structured.get("appellant_points", [])
            agency_points = structured.get("agency_points", [])
        except Exception:  # noqa
            pass
    if not appellant_points:
        appellant_points = [item.get("appellant_text", "")]
    if not agency_points:
        agency_points = [item.get("agency_text", "")]

    out = invoke_pipeline({
        "stage": "perspective",
        "payload": {"appellant_points": appellant_points, "agency_points": agency_points},
    })
    _audit("perspective", case_id)
    return _resp(200, out.get("result", {}))


def perspective_extra(case_id, body):
    """多元視角延伸，依 part 分派到單一快速類別（angles|court），避免 API Gateway 逾時。"""
    part = (body or {}).get("part", "angles")
    stage_map = {"angles": "perspective_angles", "court": "perspective_court"}
    stage = stage_map.get(part, "perspective_angles")
    r = ddb.Table(CASES_TABLE).get_item(Key={"case_id": case_id})
    if "Item" not in r:
        return _resp(404, {"error": "case not found"})
    item = r["Item"]
    appellant_points, agency_points = [], []
    if item.get("result_json"):
        try:
            structured = json.loads(item["result_json"]).get("structured", {})
            appellant_points = structured.get("appellant_points", [])
            agency_points = structured.get("agency_points", [])
        except Exception:  # noqa
            pass
    if not appellant_points:
        appellant_points = [item.get("appellant_text", "")]
    if not agency_points:
        agency_points = [item.get("agency_text", "")]
    out = invoke_pipeline({
        "stage": stage,
        "payload": {"appellant_points": appellant_points, "agency_points": agency_points},
    })
    return _resp(200, out.get("result", {}))


def perspective_chat(case_id, body):
    """多元視角討論：承辦人員追問，AI 依卷內與一般法學通識回覆。"""
    question = body.get("question", "")
    context = body.get("context", {})
    history = body.get("history", [])
    if not question:
        return _resp(400, {"error": "question required"})
    out = invoke_pipeline({
        "stage": "perspective_chat",
        "payload": {"question": question, "context": context, "history": history},
    })
    _audit("perspective_chat", case_id)
    return _resp(200, out.get("result", {}))


def classify(body):
    """上傳檔案後即時以 AI 辨識案件類別（不需 case_id）。"""
    text = (body or {}).get("text", "")
    if not text:
        return _resp(400, {"error": "text required"})
    out = invoke_pipeline({"stage": "classify", "payload": {"text": text}})
    return _resp(200, out.get("result", {}))


def get_document(body):
    """回傳指定 S3 來源檔（法條全文或案例）的內容或 presigned URL。
    body: {"s3_uri": "s3://bucket/key", "mode": "text"|"url"}"""
    uri = body.get("s3_uri", "")
    mode = body.get("mode", "text")
    if not uri.startswith("s3://"):
        return _resp(400, {"error": "invalid s3_uri"})
    without = uri[5:]
    bucket, _, key = without.partition("/")
    # 安全：僅允許讀取法規來源 bucket
    if bucket != KB_SOURCE_BUCKET:
        return _resp(403, {"error": "bucket not allowed"})

    # md → 優先對應原始 PDF（資料集同時有 md 與 pdf；規則：/md/→/pdf/，結尾 .md→.pdf）
    # 僅適用判決書（相關判決書/md/）。法規（法條/md/）一律以 md 文字內容呈現，不轉 PDF。
    if ("/md/" in key) and key.endswith(".md") and key.startswith("相關判決書/md/"):
        pdf_key = key.replace("/md/", "/pdf/")
        pdf_key = pdf_key[:-3] + ".pdf"
        try:
            s3.head_object(Bucket=bucket, Key=pdf_key)
            url = s3.generate_presigned_url("get_object",
                                            Params={"Bucket": bucket, "Key": pdf_key}, ExpiresIn=900)
            return _resp(200, {"url": url, "is_pdf": True})
        except Exception:  # noqa
            pass  # 無對應 pdf，續走原本邏輯（回文字）

    if mode == "url":
        try:
            url = s3.generate_presigned_url("get_object",
                                            Params={"Bucket": bucket, "Key": key}, ExpiresIn=900)
            return _resp(200, {"url": url})
        except Exception as e:  # noqa
            return _resp(500, {"error": str(e)[:200]})
    # text 模式：讀取檔案內容（限文字類，PDF 回傳 presigned URL）
    if key.lower().endswith(".pdf"):
        try:
            url = s3.generate_presigned_url("get_object",
                                            Params={"Bucket": bucket, "Key": key}, ExpiresIn=900)
            return _resp(200, {"url": url, "is_pdf": True})
        except Exception as e:  # noqa
            return _resp(500, {"error": str(e)[:200]})
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        text = obj["Body"].read().decode("utf-8", errors="replace")
        return _resp(200, {"text": text[:50000], "key": key})
    except Exception as e:  # noqa
        return _resp(500, {"error": str(e)[:200]})


def revise_draft(case_id, body):
    """草稿指令式修訂：承辦人員自然語言指令 → 呼叫 pipeline revise 階段修改指定欄位。"""
    instruction = body.get("instruction", "")
    target = body.get("target", "reasons")  # main|facts|reasons|notice
    if not instruction:
        return _resp(400, {"error": "instruction required"})
    r = ddb.Table(CASES_TABLE).get_item(Key={"case_id": case_id})
    if "Item" not in r:
        return _resp(404, {"error": "case not found"})
    item = r["Item"]
    current_draft = {}
    if item.get("result_json"):
        try:
            current_draft = json.loads(item["result_json"]).get("draft", {})
        except Exception:  # noqa
            pass
    out = invoke_pipeline({
        "stage": "revise",
        "payload": {"current_draft": current_draft, "instruction": instruction, "target": target},
    })
    result = out.get("result", {})
    revised = result.get("revised_draft", current_draft)
    # 寫回更新後的草稿
    try:
        full = json.loads(item.get("result_json") or "{}")
        full["draft"] = revised
        ddb.Table(CASES_TABLE).update_item(
            Key={"case_id": case_id},
            UpdateExpression="SET result_json=:r",
            ExpressionAttributeValues={":r": json.dumps(full, ensure_ascii=False)},
        )
    except Exception:  # noqa
        pass
    _audit("revise", case_id)
    # 取出被修改欄位的新內容，方便前端直接更新顯示
    field_map = {"main": "main_text", "facts": "facts_content",
                 "reasons": "reasons_content", "notice": "notice_content"}
    tf = field_map.get(target, "reasons_content")
    return _resp(200, {
        "target": target,
        "updated_content": (revised or {}).get(tf, ""),
        "warning": result.get("warning"),
        "revised_draft": revised,
    })


def _extract_case_id(path):
    """從 /api/v1/cases/{case_id}/... 解析 case_id（proxy 整合無 pathParameters）。"""
    import re
    m = re.search(r"/cases/([^/]+)", path)
    return m.group(1) if m else None


def handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except Exception:  # noqa
            body = {}

    if method == "OPTIONS":
        return _resp(200, {})

    case_id = _extract_case_id(path)

    try:
        if method == "POST" and path.rstrip("/") == "/api/v1/cases":
            return create_case(body)
        if method == "POST" and path.rstrip("/") == "/api/v1/classify":
            return classify(body)
        if method == "POST" and path.rstrip("/") == "/api/v1/document":
            return get_document(body)
        if method == "POST" and path.endswith("/perspective-analysis/chat"):
            return perspective_chat(case_id, body)
        if method == "POST" and path.endswith("/perspective-analysis/extra"):
            return perspective_extra(case_id, body)
        if method == "POST" and path.endswith("/perspective-analysis"):
            return perspective_analysis(case_id)
        if method == "POST" and path.endswith("/revise"):
            return revise_draft(case_id, body)
        if method == "GET" and path.endswith("/analyze/status"):
            return analyze_status(case_id)
        if method == "POST" and path.endswith("/analyze"):
            return analyze(case_id)
        if method == "GET" and path.endswith("/result"):
            return get_result(case_id)
        return _resp(404, {"error": "route not found", "path": path, "method": method})
    except Exception as e:  # noqa
        import traceback
        return _resp(500, {"error": str(e), "trace": traceback.format_exc()[-500:]})
