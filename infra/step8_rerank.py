# -*- coding: utf-8 -*-
"""部署 SageMaker Cross-Encoder rerank 端點。
用純 boto3 + HuggingFace TEI DLC（無需 sagemaker SDK，避開 SDK v3 移除 jumpstart 的問題）。
模型：BAAI/bge-reranker-v2-m3（多語言含中文）。執行個體：ml.g4dn.xlarge（GPU，只開1台）。
此為持續計費資源。結果寫入 step8_result.txt。Idempotent。
"""
import json
import time
from botocore.exceptions import ClientError
from common import client, slp, load_state, set_state, Logger, REGION, PREFIX, get_account_id

L = Logger("step8_result.txt")

ENDPOINT = f"{PREFIX}-rerank"
ENDPOINT_CFG = f"{PREFIX}-rerank-cfg"
MODEL_NAME = f"{PREFIX}-rerank-model"
INSTANCE = "ml.g4dn.xlarge"
HF_MODEL = "BAAI/bge-reranker-v2-m3"
# TEI GPU DLC（us-west-2 專屬 DLC 帳號 246618743249，由 SDK v3 image_uris resolver 確認）
TEI_IMAGE = "246618743249.dkr.ecr.us-west-2.amazonaws.com/tei:2.0.1-tei1.8.2-gpu-py310-cu122-ubuntu22.04"


def ensure_sagemaker_role():
    iam = client("iam")
    st = load_state()
    if st.get("sagemaker_role_arn"):
        return st["sagemaker_role_arn"]
    name = f"{PREFIX}-sagemaker-role"
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        L.log(f"[SKIP] SageMaker 角色已存在 {name}")
    except ClientError:
        r = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow",
                               "Principal": {"Service": "sagemaker.amazonaws.com"},
                               "Action": "sts:AssumeRole"}],
            }),
            Description="SageMaker execution role for NTPC rerank",
            Tags=[{"Key": "project", "Value": PREFIX}],
        )
        arn = r["Role"]["Arn"]
        iam.attach_role_policy(RoleName=name,
                               PolicyArn="arn:aws:iam::aws:policy/AmazonSageMakerFullAccess")
        L.log(f"[OK] 建立 SageMaker 角色 {name}")
        time.sleep(12)
    set_state("sagemaker_role_arn", arn)
    return arn


def main():
    acc = get_account_id()
    sm = client("sagemaker")

    # 端點已存在？
    try:
        d = sm.describe_endpoint(EndpointName=ENDPOINT)
        L.log(f"[SKIP] 端點已存在 {ENDPOINT}，status={d['EndpointStatus']}")
        set_state("rerank_endpoint_name", ENDPOINT)
        set_state("rerank_model_id", HF_MODEL)
        L.log("STEP8 DONE")
        return
    except ClientError:
        pass
    slp()

    role_arn = ensure_sagemaker_role()

    # 1. Model
    try:
        sm.describe_model(ModelName=MODEL_NAME)
        L.log(f"[SKIP] model 已存在 {MODEL_NAME}")
    except ClientError:
        sm.create_model(
            ModelName=MODEL_NAME,
            PrimaryContainer={
                "Image": TEI_IMAGE,
                "Environment": {
                    "HF_MODEL_ID": HF_MODEL,
                    "AUTO_TRUNCATE": "true",
                    "MAX_CLIENT_BATCH_SIZE": "32",
                },
            },
            ExecutionRoleArn=role_arn,
            Tags=[{"Key": "project", "Value": PREFIX}],
        )
        L.log(f"[OK] 建立 model {MODEL_NAME}（TEI {HF_MODEL}）")
    slp()

    # 2. Endpoint config
    try:
        sm.describe_endpoint_config(EndpointConfigName=ENDPOINT_CFG)
        L.log(f"[SKIP] endpoint config 已存在 {ENDPOINT_CFG}")
    except ClientError:
        sm.create_endpoint_config(
            EndpointConfigName=ENDPOINT_CFG,
            ProductionVariants=[{
                "VariantName": "default",
                "ModelName": MODEL_NAME,
                "InstanceType": INSTANCE,
                "InitialInstanceCount": 1,
            }],
            Tags=[{"Key": "project", "Value": PREFIX}],
        )
        L.log(f"[OK] 建立 endpoint config {ENDPOINT_CFG}（{INSTANCE} x1）")
    slp()

    # 3. Endpoint（非同步建立，需數分鐘 InService）
    sm.create_endpoint(EndpointName=ENDPOINT, EndpointConfigName=ENDPOINT_CFG,
                       Tags=[{"Key": "project", "Value": PREFIX}])
    L.log(f"[OK] 建立端點 {ENDPOINT}（建立中，需數分鐘 InService）")
    set_state("rerank_endpoint_name", ENDPOINT)
    set_state("rerank_model_id", HF_MODEL)
    set_state("rerank_instance", INSTANCE)
    L.log("STEP8 SUBMITTED")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
