# -*- coding: utf-8 -*-
"""部署 Custom Chunking Lambda + 建立 Bedrock Knowledge Base + S3 data source（custom transformation）。
Idempotent。結果寫入 step7_result.txt。
"""
import io
import json
import os
import time
import zipfile
from botocore.exceptions import ClientError
from common import client, slp, get_account_id, load_state, set_state, Logger, REGION, PREFIX

L = Logger("step7_result.txt")

CHUNK_FN = f"{PREFIX}-chunking"
KB_NAME = f"{PREFIX}-kb"
DS_NAME = f"{PREFIX}-s3-source"
HANDLER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lambda_src", "chunking")


def zip_dir(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, path))
    buf.seek(0)
    return buf.read()


def deploy_chunk_lambda(acc):
    lam = client("lambda")
    st = load_state()
    role_arn = st["lambda_role_arn"]
    code = zip_dir(HANDLER_DIR)
    try:
        lam.get_function(FunctionName=CHUNK_FN)
        lam.update_function_code(FunctionName=CHUNK_FN, ZipFile=code)
        L.log(f"[OK] 更新 chunking Lambda 程式碼 {CHUNK_FN}")
        slp()
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        # 建立（角色可能需傳播，重試）
        last = None
        for i in range(6):
            try:
                lam.create_function(
                    FunctionName=CHUNK_FN,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="handler.handler",
                    Code={"ZipFile": code},
                    Timeout=300,
                    MemorySize=512,
                    Tags={"project": PREFIX},
                )
                L.log(f"[OK] 建立 chunking Lambda {CHUNK_FN}")
                last = None
                break
            except ClientError as ce:
                last = ce.response["Error"]["Code"]
                L.log(f"  create_function 重試 {i+1}/6: {last}")
                time.sleep(10)
        if last:
            raise RuntimeError(f"create chunking lambda failed: {last}")
        slp()

    fn_arn = lam.get_function(FunctionName=CHUNK_FN)["Configuration"]["FunctionArn"]
    # 允許 Bedrock 呼叫此 Lambda
    try:
        lam.add_permission(
            FunctionName=CHUNK_FN,
            StatementId="bedrock-invoke",
            Action="lambda:InvokeFunction",
            Principal="bedrock.amazonaws.com",
            SourceAccount=acc,
        )
        L.log("[OK] 加上 Bedrock 可呼叫 chunking Lambda 的權限")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            L.log("[SKIP] Bedrock invoke 權限已存在")
        else:
            L.log(f"[WARN] add_permission: {e.response['Error']['Code']}")
    slp()
    set_state("chunking_lambda_arn", fn_arn)
    return fn_arn


def create_kb(acc):
    bra = client("bedrock-agent")
    st = load_state()

    # 檢查是否已存在
    try:
        for kb in bra.list_knowledge_bases(maxResults=100).get("knowledgeBaseSummaries", []):
            if kb.get("name") == KB_NAME:
                kb_id = kb["knowledgeBaseId"]
                L.log(f"[SKIP] KB 已存在 {KB_NAME} -> {kb_id}")
                set_state("kb_id", kb_id)
                return kb_id
    except ClientError as e:
        L.log(f"[WARN] list_knowledge_bases: {e}")
    slp()

    embed_arn = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"
    last = None
    for i in range(8):
        try:
            resp = bra.create_knowledge_base(
                name=KB_NAME,
                description="新北市訴願法規/函釋/決定書向量知識庫",
                roleArn=st["kb_role_arn"],
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": embed_arn,
                        "embeddingModelConfiguration": {
                            "bedrockEmbeddingModelConfiguration": {"dimensions": 1024}
                        },
                    },
                },
                storageConfiguration={
                    "type": "OPENSEARCH_SERVERLESS",
                    "opensearchServerlessConfiguration": {
                        "collectionArn": st["aoss_arn"],
                        "vectorIndexName": st["aoss_index_name"],
                        "fieldMapping": {
                            "vectorField": st["aoss_vector_field"],
                            "textField": st["aoss_text_field"],
                            "metadataField": st["aoss_meta_field"],
                        },
                    },
                },
                tags={"project": PREFIX},
            )
            kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
            L.log(f"[OK] 建立 Knowledge Base {KB_NAME} -> {kb_id}")
            last = None
            break
        except ClientError as e:
            last = f"{e.response['Error']['Code']}: {e.response['Error']['Message'][:150]}"
            L.log(f"  create_knowledge_base 重試 {i+1}/8: {last}")
            time.sleep(15)  # 等 role/index/data-access 傳播
    if last:
        raise RuntimeError(f"create KB failed: {last}")
    slp()
    set_state("kb_id", kb_id)
    return kb_id


def create_data_source(acc, kb_id):
    bra = client("bedrock-agent")
    st = load_state()
    kb_bucket = st["kb_bucket"]
    art_bucket = st["artifacts_bucket"]
    chunk_arn = st["chunking_lambda_arn"]

    # 檢查是否已存在
    try:
        for ds in bra.list_data_sources(knowledgeBaseId=kb_id, maxResults=100).get("dataSourceSummaries", []):
            if ds.get("name") == DS_NAME:
                L.log(f"[SKIP] data source 已存在 {DS_NAME} -> {ds['dataSourceId']}")
                set_state("kb_data_source_id", ds["dataSourceId"])
                return ds["dataSourceId"]
    except ClientError as e:
        L.log(f"[WARN] list_data_sources: {e}")
    slp()

    resp = bra.create_data_source(
        knowledgeBaseId=kb_id,
        name=DS_NAME,
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {"bucketArn": f"arn:aws:s3:::{kb_bucket}"},
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {"chunkingStrategy": "NONE"},
            "customTransformationConfiguration": {
                "intermediateStorage": {
                    "s3Location": {"uri": f"s3://{art_bucket}/kb-intermediate/"}
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
    L.log(f"[OK] 建立 data source {DS_NAME} -> {ds_id}（S3 + custom POST_CHUNKING transform）")
    set_state("kb_data_source_id", ds_id)
    slp()
    return ds_id


def main():
    acc = get_account_id()
    deploy_chunk_lambda(acc)
    kb_id = create_kb(acc)
    # KB 建立後短暫等待再建 data source
    time.sleep(10)
    create_data_source(acc, kb_id)
    L.log("STEP7 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
