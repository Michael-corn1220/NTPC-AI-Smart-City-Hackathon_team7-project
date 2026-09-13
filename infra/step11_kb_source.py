# -*- coding: utf-8 -*-
"""將 Knowledge Base 接上 legal-dataset-hackathon bucket：
1) 給 KB 服務角色加讀取該 bucket 的權限
2) 新增指向該 bucket 的 data source（沿用 Custom Chunking Lambda）
3) 觸發 ingestion job
Idempotent。結果寫入 step11_result.txt。
"""
import json
import time
from botocore.exceptions import ClientError
from common import client, slp, load_state, set_state, Logger, REGION, PREFIX, get_account_id

L = Logger("step11_result.txt")
LEGAL_BUCKET = "legal-dataset-hackathon-191149991440"
DS_NAME = f"{PREFIX}-legal-source"


def add_bucket_perm(acc):
    """KB 服務角色補上讀 legal-dataset bucket 的權限。"""
    iam = client("iam")
    role = f"{PREFIX}-kb-role"
    st = load_state()
    kb_bucket = st["kb_bucket"]
    kms_key = st["kms_key_id"]
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "InvokeEmbed", "Effect": "Allow", "Action": ["bedrock:InvokeModel"],
             "Resource": [f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"]},
            {"Sid": "Aoss", "Effect": "Allow", "Action": ["aoss:APIAccessAll"],
             "Resource": [st["aoss_arn"]]},
            {"Sid": "S3Read", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": [
                 f"arn:aws:s3:::{kb_bucket}", f"arn:aws:s3:::{kb_bucket}/*",
                 f"arn:aws:s3:::{LEGAL_BUCKET}", f"arn:aws:s3:::{LEGAL_BUCKET}/*",
             ]},
            # custom transformation 中間儲存需讀寫 artifacts bucket
            {"Sid": "S3Intermediate", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"],
             "Resource": [
                 f"arn:aws:s3:::{st['artifacts_bucket']}",
                 f"arn:aws:s3:::{st['artifacts_bucket']}/*",
             ]},
            {"Sid": "Kms", "Effect": "Allow",
             "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
             "Resource": [f"arn:aws:kms:{REGION}:{acc}:key/{kms_key}"]},
        ],
    }
    iam.put_role_policy(RoleName=role, PolicyName="kb-perms",
                        PolicyDocument=json.dumps(doc))
    L.log(f"[OK] KB 角色補上讀 {LEGAL_BUCKET} 權限")
    slp()


def ensure_data_source(kb_id):
    bra = client("bedrock-agent")
    st = load_state()
    chunk_arn = st["chunking_lambda_arn"]
    art_bucket = st["artifacts_bucket"]

    # 已存在？
    try:
        for ds in bra.list_data_sources(knowledgeBaseId=kb_id, maxResults=100).get("dataSourceSummaries", []):
            if ds.get("name") == DS_NAME:
                L.log(f"[SKIP] data source 已存在 {DS_NAME} -> {ds['dataSourceId']}")
                set_state("legal_data_source_id", ds["dataSourceId"])
                return ds["dataSourceId"]
    except ClientError as e:
        L.log(f"[WARN] list_data_sources: {e}")
    slp()

    resp = bra.create_data_source(
        knowledgeBaseId=kb_id,
        name=DS_NAME,
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {"bucketArn": f"arn:aws:s3:::{LEGAL_BUCKET}"},
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {"chunkingStrategy": "NONE"},
            "customTransformationConfiguration": {
                "intermediateStorage": {
                    "s3Location": {"uri": f"s3://{art_bucket}/kb-legal-intermediate/"}
                },
                "transformations": [{
                    "stepToApply": "POST_CHUNKING",
                    "transformationFunction": {
                        "transformationLambdaConfiguration": {"lambdaArn": chunk_arn}
                    },
                }],
            },
        },
    )
    ds_id = resp["dataSource"]["dataSourceId"]
    L.log(f"[OK] 建立 data source {DS_NAME} -> {ds_id}（bucket={LEGAL_BUCKET}）")
    set_state("legal_data_source_id", ds_id)
    slp()
    return ds_id


def start_ingestion(kb_id, ds_id):
    bra = client("bedrock-agent")
    try:
        resp = bra.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                       description="ingest legal-dataset-hackathon")
        job = resp["ingestionJob"]
        L.log(f"[OK] 觸發 ingestion job {job['ingestionJobId']} status={job['status']}")
        set_state("legal_ingestion_job_id", job["ingestionJobId"])
        return job["ingestionJobId"]
    except ClientError as e:
        L.log(f"[FAIL] start_ingestion_job: {e.response['Error']['Code']}: {e.response['Error'].get('Message','')[:150]}")
        return None


def main():
    acc = get_account_id()
    st = load_state()
    kb_id = st["kb_id"]
    add_bucket_perm(acc)
    time.sleep(8)  # 角色政策傳播
    ds_id = ensure_data_source(kb_id)
    start_ingestion(kb_id, ds_id)
    L.log("STEP11 SUBMITTED（ingestion 為非同步，906 檔案需數分鐘）")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
