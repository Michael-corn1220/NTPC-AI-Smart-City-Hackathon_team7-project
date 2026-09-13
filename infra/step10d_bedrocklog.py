# -*- coding: utf-8 -*-
"""step10d：開啟 Bedrock model invocation logging 到 CloudWatch Logs（us-west-2）。
- 建 log group /ntpc-appeals/bedrock-invocations（已建則略過）
- 建專用 IAM role（trust bedrock.amazonaws.com）+ inline policy 允許寫該 log group
- 設 Bedrock put_model_invocation_logging_configuration 指向 log group + role
"""
import json
import time
import boto3
from botocore.config import Config
from common import Logger, set_state

REGION_W = "us-west-2"
ACCOUNT = "191149991440"
LOG_GROUP = "/ntpc-appeals/bedrock-invocations"
ROLE_NAME = "ntpc-appeals-bedrock-logging-role"

cfg_w = Config(region_name=REGION_W, retries={"max_attempts": 5, "mode": "standard"})
logs = boto3.client("logs", config=cfg_w)
brw = boto3.client("bedrock", config=cfg_w)
iam = boto3.client("iam", config=cfg_w)
L = Logger("step10d_bedrocklog_result.txt")


def ensure_log_group():
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        L.log(f"[OK] 建立 log group {LOG_GROUP}")
    except logs.exceptions.ResourceAlreadyExistsException:
        L.log(f"[SKIP] log group 已存在 {LOG_GROUP}")
    try:
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
        L.log("[OK] 設定保留 30 天")
    except Exception as e:
        L.log("[WARN] 保留設定: " + str(e)[:120])


def ensure_role():
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{REGION_W}:{ACCOUNT}:*"},
            },
        }],
    }
    try:
        r = iam.create_role(RoleName=ROLE_NAME,
                            AssumeRolePolicyDocument=json.dumps(trust),
                            Description="Bedrock model invocation logging to CloudWatch")
        arn = r["Role"]["Arn"]
        L.log(f"[OK] 建立 role {arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        L.log(f"[SKIP] role 已存在 {arn}")
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
    perm = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"arn:aws:logs:{REGION_W}:{ACCOUNT}:log-group:{LOG_GROUP}:*",
        }],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="write-bedrock-logs",
                        PolicyDocument=json.dumps(perm))
    L.log("[OK] 附加 inline policy（寫入 log group）")
    set_state("bedrock_logging_role_arn", arn)
    return arn


def set_bedrock_logging(role_arn):
    brw.put_model_invocation_logging_configuration(loggingConfig={
        "cloudWatchConfig": {"logGroupName": LOG_GROUP, "roleArn": role_arn},
        "textDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "embeddingDataDeliveryEnabled": False,
    })
    L.log("[OK] 開啟 Bedrock model invocation logging（CloudWatch，textData=on）")


def main():
    L.log("===== step10d Bedrock invocation logging =====")
    ensure_log_group()
    role_arn = ensure_role()
    # IAM 角色傳播需等待，重試
    last = None
    for i in range(8):
        try:
            set_bedrock_logging(role_arn)
            last = None
            break
        except Exception as e:
            last = e
            L.log(f"[..] 等待角色傳播重試 {i+1}/8: {str(e)[:100]}")
            time.sleep(8)
    cfg = brw.get_model_invocation_logging_configuration().get("loggingConfig", {})
    L.log("目前 Bedrock logging config: " + json.dumps(cfg, ensure_ascii=False))
    if last:
        L.log("[FAIL] 最終仍失敗: " + str(last)[:200])
    L.log("")
    L.log("STEP10D BEDROCK LOG DONE")
    L.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
        L.flush()
