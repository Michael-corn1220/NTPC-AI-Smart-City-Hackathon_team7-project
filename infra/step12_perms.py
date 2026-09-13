# -*- coding: utf-8 -*-
"""補 lambda-role 讀取 legal-dataset bucket 的權限（get_document 端點需要）。Idempotent。"""
import json
from common import client, load_state, Logger, REGION, PREFIX, get_account_id

L = Logger("step12_result.txt")
LEGAL = "legal-dataset-hackathon-191149991440"


def main():
    acc = get_account_id()
    st = load_state()
    iam = client("iam")
    role = f"{PREFIX}-lambda-role"
    kb_bucket = st["kb_bucket"]; art = st["artifacts_bucket"]; kms = st["kms_key_id"]
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Bedrock", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:Retrieve", "bedrock:RetrieveAndGenerate",
                        "bedrock:ApplyGuardrail"], "Resource": "*"},
            {"Sid": "Comprehend", "Effect": "Allow",
             "Action": ["comprehend:DetectPiiEntities", "comprehend:DetectEntities"], "Resource": "*"},
            {"Sid": "Ddb", "Effect": "Allow",
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                        "dynamodb:Query", "dynamodb:Scan"],
             "Resource": [f"arn:aws:dynamodb:{REGION}:{acc}:table/{PREFIX}-*"]},
            {"Sid": "S3", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{kb_bucket}", f"arn:aws:s3:::{kb_bucket}/*",
                          f"arn:aws:s3:::{art}", f"arn:aws:s3:::{art}/*",
                          f"arn:aws:s3:::{LEGAL}", f"arn:aws:s3:::{LEGAL}/*"]},
            {"Sid": "Sagemaker", "Effect": "Allow",
             "Action": ["sagemaker:InvokeEndpoint"], "Resource": "*"},
            {"Sid": "Kms", "Effect": "Allow",
             "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
             "Resource": [f"arn:aws:kms:{REGION}:{acc}:key/{kms}"]},
            {"Sid": "Sfn", "Effect": "Allow",
             "Action": ["states:StartExecution", "states:DescribeExecution",
                        "states:SendTaskSuccess", "states:SendTaskFailure"], "Resource": "*"},
            {"Sid": "InvokeLambda", "Effect": "Allow",
             "Action": ["lambda:InvokeFunction"],
             "Resource": [f"arn:aws:lambda:{REGION}:{acc}:function:{PREFIX}-*"]},
        ],
    }
    iam.put_role_policy(RoleName=role, PolicyName="lambda-perms", PolicyDocument=json.dumps(doc))
    L.log("[OK] lambda-role 補上讀 legal-dataset bucket 權限")
    L.log("STEP12 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
