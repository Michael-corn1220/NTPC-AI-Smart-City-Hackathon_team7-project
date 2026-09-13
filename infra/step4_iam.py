# -*- coding: utf-8 -*-
"""建立 IAM 角色：Bedrock KB 服務角色、Lambda 執行角色、Step Functions 角色。Idempotent。
並更新 AOSS data access policy 納入 KB 服務角色，檢查 collection 狀態。
"""
import json
import time
from botocore.exceptions import ClientError
from common import client, slp, get_account_id, load_state, set_state, Logger, PREFIX, REGION

L = Logger("step4_result.txt")


def ensure_role(iam, name, assume_doc, desc):
    try:
        r = iam.get_role(RoleName=name)
        L.log(f"[SKIP] IAM 角色已存在 {name}")
        return r["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    slp()
    r = iam.create_role(
        RoleName=name,
        AssumeRolePolicyDocument=json.dumps(assume_doc),
        Description=desc,
        Tags=[{"Key": "project", "Value": PREFIX}],
    )
    L.log(f"[OK] 建立 IAM 角色 {name}")
    slp()
    return r["Role"]["Arn"]


def put_inline(iam, role, pname, doc):
    iam.put_role_policy(RoleName=role, PolicyName=pname, PolicyDocument=json.dumps(doc))
    L.log(f"[OK] 附加 inline policy {pname} -> {role}")
    slp()


def attach_managed(iam, role, arn):
    iam.attach_role_policy(RoleName=role, PolicyArn=arn)
    L.log(f"[OK] 附加 managed policy {arn.split('/')[-1]} -> {role}")
    slp()


def main():
    acc = get_account_id()
    st = load_state()
    iam = client("iam")
    kb_bucket = st["kb_bucket"]
    art_bucket = st["artifacts_bucket"]
    kms_key = st["kms_key_id"]
    aoss_arn = st.get("aoss_arn") or f"arn:aws:aoss:{REGION}:{acc}:collection/{st['aoss_collection_id']}"

    # ---- 1. Bedrock KB 服務角色 ----
    kb_role_name = f"{PREFIX}-kb-role"
    kb_arn = ensure_role(iam, kb_role_name, {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": acc}},
        }],
    }, "Bedrock Knowledge Base service role")
    put_inline(iam, kb_role_name, "kb-perms", {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "InvokeEmbed", "Effect": "Allow", "Action": ["bedrock:InvokeModel"],
             "Resource": [f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"]},
            {"Sid": "Aoss", "Effect": "Allow", "Action": ["aoss:APIAccessAll"],
             "Resource": [aoss_arn]},
            {"Sid": "S3Read", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{kb_bucket}", f"arn:aws:s3:::{kb_bucket}/*"]},
            {"Sid": "Kms", "Effect": "Allow",
             "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
             "Resource": [f"arn:aws:kms:{REGION}:{acc}:key/{kms_key}"]},
        ],
    })
    set_state("kb_role_arn", kb_arn)

    # ---- 2. Lambda 執行角色 ----
    lam_role_name = f"{PREFIX}-lambda-role"
    lam_arn = ensure_role(iam, lam_role_name, {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    }, "Lambda execution role for NTPC appeals")
    attach_managed(iam, lam_role_name, "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    put_inline(iam, lam_role_name, "lambda-perms", {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Bedrock", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:Retrieve", "bedrock:RetrieveAndGenerate",
                        "bedrock:ApplyGuardrail"],
             "Resource": "*"},
            {"Sid": "Comprehend", "Effect": "Allow",
             "Action": ["comprehend:DetectPiiEntities", "comprehend:DetectEntities"],
             "Resource": "*"},
            {"Sid": "Ddb", "Effect": "Allow",
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                        "dynamodb:Query", "dynamodb:Scan"],
             "Resource": [f"arn:aws:dynamodb:{REGION}:{acc}:table/{PREFIX}-*"]},
            {"Sid": "S3", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{kb_bucket}", f"arn:aws:s3:::{kb_bucket}/*",
                          f"arn:aws:s3:::{art_bucket}", f"arn:aws:s3:::{art_bucket}/*"]},
            {"Sid": "Sagemaker", "Effect": "Allow",
             "Action": ["sagemaker:InvokeEndpoint"], "Resource": "*"},
            {"Sid": "Kms", "Effect": "Allow",
             "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
             "Resource": [f"arn:aws:kms:{REGION}:{acc}:key/{kms_key}"]},
            {"Sid": "SfnCallback", "Effect": "Allow",
             "Action": ["states:SendTaskSuccess", "states:SendTaskFailure"], "Resource": "*"},
        ],
    })
    set_state("lambda_role_arn", lam_arn)

    # ---- 3. Step Functions 角色 ----
    sfn_role_name = f"{PREFIX}-sfn-role"
    sfn_arn = ensure_role(iam, sfn_role_name, {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    }, "Step Functions role for NTPC appeals pipeline")
    put_inline(iam, sfn_role_name, "sfn-perms", {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "InvokeLambda", "Effect": "Allow", "Action": ["lambda:InvokeFunction"],
             "Resource": [f"arn:aws:lambda:{REGION}:{acc}:function:{PREFIX}-*"]},
            {"Sid": "Sqs", "Effect": "Allow",
             "Action": ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
                        "sqs:GetQueueAttributes"], "Resource": "*"},
        ],
    })
    set_state("sfn_role_arn", sfn_arn)

    # ---- 4. 更新 AOSS data access policy 納入 KB 角色 ----
    aoss = client("opensearchserverless")
    coll = st["aoss_collection_name"]
    caller = client("sts").get_caller_identity()["Arn"]
    access_name = f"{PREFIX}-access"
    try:
        cur = aoss.get_access_policy(name=access_name, type="data")
        version = cur["accessPolicyDetail"]["policyVersion"]
        policy = [{
            "Rules": [
                {"ResourceType": "collection", "Resource": [f"collection/{coll}"],
                 "Permission": ["aoss:CreateCollectionItems", "aoss:DeleteCollectionItems",
                                "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems"]},
                {"ResourceType": "index", "Resource": [f"index/{coll}/*"],
                 "Permission": ["aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                                "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"]},
            ],
            "Principal": [caller, kb_arn],
        }]
        aoss.update_access_policy(name=access_name, type="data",
                                  policyVersion=version, policy=json.dumps(policy))
        L.log(f"[OK] 更新 AOSS data access policy 納入 KB 角色")
        slp()
    except ClientError as e:
        L.log(f"[WARN] update_access_policy: {e}")

    # ---- 5. 檢查 collection 狀態 ----
    try:
        d = aoss.batch_get_collection(ids=[st["aoss_collection_id"]])
        det = d.get("collectionDetails", [])
        if det:
            L.log(f"  collection status={det[0].get('status')}, endpoint={det[0].get('collectionEndpoint')}")
            if det[0].get("collectionEndpoint"):
                set_state("aoss_endpoint", det[0]["collectionEndpoint"])
            if det[0].get("arn"):
                set_state("aoss_arn", det[0]["arn"])
    except ClientError as e:
        L.log(f"[WARN] batch_get_collection: {e}")

    L.log("STEP4 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
