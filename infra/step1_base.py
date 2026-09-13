# -*- coding: utf-8 -*-
"""建立基礎資源：KMS 金鑰、S3 buckets、DynamoDB 三表。Idempotent。"""
import json
from botocore.exceptions import ClientError
from common import client, slp, get_account_id, load_state, set_state, Logger, REGION, PREFIX

L = Logger("step1_result.txt")


def ensure_kms():
    kms = client("kms")
    alias = f"alias/{PREFIX}"
    # 檢查別名是否已存在
    try:
        aliases = kms.list_aliases()
        for a in aliases.get("Aliases", []):
            if a.get("AliasName") == alias:
                key_id = a.get("TargetKeyId")
                L.log(f"[SKIP] KMS 別名已存在 {alias} -> {key_id}")
                set_state("kms_key_id", key_id)
                return key_id
    except ClientError as e:
        L.log(f"[WARN] list_aliases: {e}")
    slp()
    # 建立金鑰
    resp = kms.create_key(
        Description="NTPC Appeals AI system encryption key",
        KeyUsage="ENCRYPT_DECRYPT",
        Tags=[{"TagKey": "project", "TagValue": PREFIX}],
    )
    key_id = resp["KeyMetadata"]["KeyId"]
    slp()
    kms.create_alias(AliasName=alias, TargetKeyId=key_id)
    L.log(f"[OK] 建立 KMS 金鑰 {key_id}，別名 {alias}")
    set_state("kms_key_id", key_id)
    slp()
    return key_id


def ensure_bucket(name, kms_key_id, prefixes=None):
    s3 = client("s3")
    # 檢查是否已存在
    try:
        s3.head_bucket(Bucket=name)
        L.log(f"[SKIP] S3 bucket 已存在 {name}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            s3.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
            L.log(f"[OK] 建立 S3 bucket {name}")
            slp()
        elif code == "403":
            L.log(f"[WARN] {name} 已被他人擁有(403)，改用帶 accountid 後綴應已避免；請檢查")
            return None
        else:
            raise
    slp()
    # Block Public Access 全開
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    slp()
    # 預設加密 SSE-KMS
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [{
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "aws:kms",
                    "KMSMasterKeyID": kms_key_id,
                },
                "BucketKeyEnabled": True,
            }]
        },
    )
    slp()
    # 版本控管（利於 ingestion ETag 比對）
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    slp()
    # 建立目錄前綴（空物件佔位）
    if prefixes:
        for p in prefixes:
            s3.put_object(Bucket=name, Key=p if p.endswith("/") else p + "/")
            slp(0.3)
    L.log(f"[OK] {name} 已設定 BlockPublicAccess + SSE-KMS + Versioning" + (f" + 前綴{prefixes}" if prefixes else ""))
    return name


def ensure_table(ddb, name, key_schema, attr_defs, kms_key_id):
    try:
        ddb.describe_table(TableName=name)
        L.log(f"[SKIP] DynamoDB 表已存在 {name}")
        return name
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    slp()
    ddb.create_table(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attr_defs,
        BillingMode="PAY_PER_REQUEST",
        SSESpecification={"Enabled": True, "SSEType": "KMS", "KMSMasterKeyId": kms_key_id},
        Tags=[{"Key": "project", "Value": PREFIX}],
    )
    L.log(f"[OK] 建立 DynamoDB 表 {name}（On-Demand + KMS）")
    slp()
    return name


def main():
    acc = get_account_id()
    L.log(f"Account: {acc}, Region: {REGION}")

    # 1. KMS
    kms_key_id = ensure_kms()

    # 2. S3
    kb_bucket = f"{PREFIX}-kb-source-{acc}"
    art_bucket = f"{PREFIX}-artifacts-{acc}"
    ensure_bucket(kb_bucket, kms_key_id,
                  prefixes=["laws/", "interpretations/", "decisions/", "penalty_tables/"])
    ensure_bucket(art_bucket, kms_key_id,
                  prefixes=["uploads/", "drafts/", "exports/"])
    set_state("kb_bucket", kb_bucket)
    set_state("artifacts_bucket", art_bucket)

    # 3. DynamoDB
    ddb = client("dynamodb")
    ensure_table(ddb, f"{PREFIX}-cases",
                 [{"AttributeName": "case_id", "KeyType": "HASH"}],
                 [{"AttributeName": "case_id", "AttributeType": "S"}], kms_key_id)
    ensure_table(ddb, f"{PREFIX}-drafts",
                 [{"AttributeName": "draft_id", "KeyType": "HASH"},
                  {"AttributeName": "case_id", "KeyType": "RANGE"}],
                 [{"AttributeName": "draft_id", "AttributeType": "S"},
                  {"AttributeName": "case_id", "AttributeType": "S"}], kms_key_id)
    ensure_table(ddb, f"{PREFIX}-audit-logs",
                 [{"AttributeName": "log_id", "KeyType": "HASH"},
                  {"AttributeName": "timestamp", "KeyType": "RANGE"}],
                 [{"AttributeName": "log_id", "AttributeType": "S"},
                  {"AttributeName": "timestamp", "AttributeType": "N"}], kms_key_id)
    set_state("ddb_tables", [f"{PREFIX}-cases", f"{PREFIX}-drafts", f"{PREFIX}-audit-logs"])

    L.log("")
    L.log("STEP1 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
