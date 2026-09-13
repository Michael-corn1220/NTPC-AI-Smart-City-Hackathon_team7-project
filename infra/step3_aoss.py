# -*- coding: utf-8 -*-
"""建立 OpenSearch Serverless collection（向量庫）+ encryption/network/data-access 政策。Idempotent。
註：AOSS 為持續計費資源（最低 OCU）。
"""
import json
from botocore.exceptions import ClientError
from common import client, slp, get_account_id, load_state, set_state, Logger, PREFIX

L = Logger("step3_result.txt")
COLLECTION = f"{PREFIX}-kb"          # collection 名稱（<=32 字元）
ENC_POLICY = f"{PREFIX}-enc"
NET_POLICY = f"{PREFIX}-net"
ACCESS_POLICY = f"{PREFIX}-access"


def ensure_security_policy(aoss, name, ptype, policy):
    try:
        aoss.get_security_policy(name=name, type=ptype)
        L.log(f"[SKIP] security policy 已存在 {name} ({ptype})")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ResourceNotFoundException",):
            L.log(f"[WARN] get_security_policy {name}: {e.response['Error']['Code']}")
    slp()
    aoss.create_security_policy(name=name, type=ptype, policy=json.dumps(policy))
    L.log(f"[OK] 建立 security policy {name} ({ptype})")
    slp()


def ensure_access_policy(aoss, name, policy):
    try:
        aoss.get_access_policy(name=name, type="data")
        L.log(f"[SKIP] access policy 已存在 {name}")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ResourceNotFoundException",):
            L.log(f"[WARN] get_access_policy {name}: {e.response['Error']['Code']}")
    slp()
    aoss.create_access_policy(name=name, type="data", policy=json.dumps(policy))
    L.log(f"[OK] 建立 data access policy {name}")
    slp()


def main():
    acc = get_account_id()
    aoss = client("opensearchserverless")
    caller_arn = client("sts").get_caller_identity()["Arn"]
    # assumed-role ARN 轉為 role ARN 供 data access（AOSS 接受 assumed-role session ARN 亦可，這裡用原始 caller）
    L.log(f"caller: {caller_arn}")

    coll_resource = [f"collection/{COLLECTION}"]
    index_resource = [f"index/{COLLECTION}/*"]

    # 1. encryption policy（用 AWS 擁有金鑰，AOSS 尚不支援自訂 KMS 於此示範用預設）
    ensure_security_policy(aoss, ENC_POLICY, "encryption", {
        "Rules": [{"ResourceType": "collection", "Resource": coll_resource}],
        "AWSOwnedKey": True,
    })

    # 2. network policy（public 端點，但 AOSS data plane 仍受 IAM/data access 控制；不涉及 S3 public）
    ensure_security_policy(aoss, NET_POLICY, "network", [{
        "Rules": [
            {"ResourceType": "collection", "Resource": coll_resource},
            {"ResourceType": "dashboard", "Resource": coll_resource},
        ],
        "AllowFromPublic": True,
    }])

    # 3. data access policy（允許目前 caller 與後續 KB 服務角色操作）
    principals = [caller_arn]
    # 也預先加入 KB 服務角色 ARN（若已建立）
    st = load_state()
    if st.get("kb_role_arn"):
        principals.append(st["kb_role_arn"])
    ensure_access_policy(aoss, ACCESS_POLICY, [{
        "Rules": [
            {"ResourceType": "collection", "Resource": coll_resource,
             "Permission": ["aoss:CreateCollectionItems", "aoss:DeleteCollectionItems",
                            "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems"]},
            {"ResourceType": "index", "Resource": index_resource,
             "Permission": ["aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                            "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"]},
        ],
        "Principal": principals,
    }])

    # 4. collection
    existing = None
    try:
        resp = aoss.list_collections()
        for c in resp.get("collectionSummaries", []):
            if c.get("name") == COLLECTION:
                existing = c.get("id")
    except ClientError as e:
        L.log(f"[WARN] list_collections: {e}")
    slp()

    if existing:
        L.log(f"[SKIP] collection 已存在 {COLLECTION} -> {existing}")
        coll_id = existing
    else:
        resp = aoss.create_collection(name=COLLECTION, type="VECTORSEARCH",
                                      description="NTPC appeals KB vector store",
                                      tags=[{"key": "project", "value": PREFIX}])
        coll_id = resp["createCollectionDetail"]["id"]
        L.log(f"[OK] 建立 collection {COLLECTION} -> {coll_id}（建立中，需數分鐘 ACTIVE）")
        slp()

    set_state("aoss_collection_name", COLLECTION)
    set_state("aoss_collection_id", coll_id)
    # 取得 endpoint（可能尚未 ready）
    try:
        d = aoss.batch_get_collection(ids=[coll_id])
        details = d.get("collectionDetails", [])
        if details:
            set_state("aoss_endpoint", details[0].get("collectionEndpoint"))
            set_state("aoss_arn", details[0].get("arn"))
            L.log(f"  status={details[0].get('status')}, endpoint={details[0].get('collectionEndpoint')}")
    except ClientError as e:
        L.log(f"[WARN] batch_get_collection: {e}")

    L.log("STEP3 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
