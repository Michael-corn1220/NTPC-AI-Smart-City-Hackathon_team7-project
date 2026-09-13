# -*- coding: utf-8 -*-
"""整體驗證：檢查所有已建資源狀態，實測 API 建立案件端點。結果寫入 verify_result.txt。"""
import json
import urllib.request
from botocore.exceptions import ClientError
from common import client, slp, load_state, Logger, REGION, PREFIX

L = Logger("verify_result.txt")


def check(label, fn):
    try:
        r = fn()
        L.log(f"[OK] {label}: {r}")
    except ClientError as e:
        L.log(f"[FAIL] {label}: {e.response['Error']['Code']}")
    except Exception as e:  # noqa
        L.log(f"[FAIL] {label}: {type(e).__name__} {str(e)[:120]}")
    slp(0.3)


def main():
    st = load_state()
    L.log("===== 資源狀態驗證 =====")

    # S3
    s3 = client("s3")
    for b in (st["kb_bucket"], st["artifacts_bucket"]):
        check(f"S3 {b} BlockPublicAccess", lambda b=b: s3.get_public_access_block(
            Bucket=b)["PublicAccessBlockConfiguration"]["BlockPublicAcls"])

    # DynamoDB
    ddb = client("dynamodb")
    for t in st["ddb_tables"]:
        check(f"DDB {t}", lambda t=t: ddb.describe_table(TableName=t)["Table"]["TableStatus"])

    # KMS
    kms = client("kms")
    check("KMS key", lambda: kms.describe_key(KeyId=st["kms_key_id"])["KeyMetadata"]["Enabled"])

    # Guardrail
    br = client("bedrock")
    check("Guardrail", lambda: br.get_guardrail(
        guardrailIdentifier=st["guardrail_id"], guardrailVersion=st["guardrail_version"])["status"])

    # AOSS
    aoss = client("opensearchserverless")
    check("AOSS collection", lambda: aoss.batch_get_collection(
        ids=[st["aoss_collection_id"]])["collectionDetails"][0]["status"])

    # KB
    bra = client("bedrock-agent")
    check("Knowledge Base", lambda: bra.get_knowledge_base(
        knowledgeBaseId=st["kb_id"])["knowledgeBase"]["status"])
    check("KB data source", lambda: bra.get_data_source(
        knowledgeBaseId=st["kb_id"], dataSourceId=st["kb_data_source_id"])["dataSource"]["status"])

    # SageMaker rerank endpoint
    sm = client("sagemaker")
    check("Rerank endpoint", lambda: sm.describe_endpoint(
        EndpointName=st["rerank_endpoint_name"])["EndpointStatus"])

    # Cognito
    cog = client("cognito-idp")
    check("Cognito pool", lambda: cog.describe_user_pool(
        UserPoolId=st["cognito_user_pool_id"])["UserPool"]["Name"])

    # Lambda
    lam = client("lambda")
    for fn in (f"{PREFIX}-chunking", f"{PREFIX}-pipeline", f"{PREFIX}-api"):
        check(f"Lambda {fn}", lambda fn=fn: lam.get_function(
            FunctionName=fn)["Configuration"]["State"])
    check("pipeline reserved concurrency", lambda: lam.get_function_concurrency(
        FunctionName=f"{PREFIX}-pipeline").get("ReservedConcurrentExecutions"))

    # Step Functions
    sfn = client("stepfunctions")
    check("State machine", lambda: sfn.describe_state_machine(
        stateMachineArn=st["state_machine_arn"])["status"])

    # API Gateway 實測（建立案件）
    L.log("")
    L.log("===== API 實測 =====")
    try:
        url = st["api_url"] + "/api/v1/cases"
        body = json.dumps({"case_no": "測試字第000號", "category": "廢棄物清理法",
                           "appellant_text": "（合成測試）訴願人主張原處分認事用法有誤。",
                           "agency_text": "（合成測試）原處分機關答辯已依法裁處。"}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
            L.log(f"[OK] POST /api/v1/cases -> {resp.status} {data[:200]}")
    except Exception as e:  # noqa
        import traceback
        L.log(f"[FAIL] API 實測: {str(e)[:200]}")

    L.log("")
    L.log("VERIFY DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
