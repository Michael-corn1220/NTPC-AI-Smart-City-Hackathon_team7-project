# -*- coding: utf-8 -*-
"""等待 AOSS collection ACTIVE 並建立 1024 維 FAISS 向量索引（供 Bedrock KB / Titan v2）。
需要 opensearch-py + requests-aws4auth。Idempotent。結果寫入 step6_result.txt。
"""
import subprocess
import sys
import time
import json
from common import client, slp, get_account_id, load_state, set_state, Logger, REGION, PREFIX

L = Logger("step6_result.txt")

INDEX_NAME = "ntpc-appeals-index"
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
META_FIELD = "AMAZON_BEDROCK_METADATA"


def ensure_deps():
    for pkg, imp in [("opensearch-py", "opensearchpy"), ("requests-aws4auth", "requests_aws4auth")]:
        try:
            __import__(imp)
        except ImportError:
            L.log(f"[INFO] 安裝 {pkg}")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg], check=False)


def wait_active(aoss, coll_id, timeout=600):
    start = time.time()
    while time.time() - start < timeout:
        d = aoss.batch_get_collection(ids=[coll_id])
        det = d.get("collectionDetails", [])
        if det:
            status = det[0].get("status")
            if status == "ACTIVE":
                return det[0].get("collectionEndpoint")
            L.log(f"  collection status={status}，等待中…")
        time.sleep(15)
    return None


def main():
    ensure_deps()
    from opensearchpy import OpenSearch, RequestsHttpConnection
    from requests_aws4auth import AWS4Auth
    import boto3

    st = load_state()
    acc = get_account_id()
    aoss = client("opensearchserverless")
    coll_id = st["aoss_collection_id"]

    endpoint = wait_active(aoss, coll_id)
    if not endpoint:
        L.log("[FAIL] collection 未在時限內 ACTIVE")
        L.log("STEP6 FAIL")
        return
    L.log(f"[OK] collection ACTIVE，endpoint={endpoint}")
    set_state("aoss_endpoint", endpoint)
    host = endpoint.replace("https://", "")

    # aws4auth（aoss 服務簽章）
    sess = boto3.Session()
    creds = sess.get_credentials().get_frozen_credentials()
    awsauth = AWS4Auth(creds.access_key, creds.secret_key, REGION, "aoss",
                       session_token=creds.token)

    os_client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=awsauth,
        use_ssl=True, verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=5, timeout=60,
    )

    # data access policy 生效可能需要短暫延遲
    time.sleep(20)

    if os_client.indices.exists(index=INDEX_NAME):
        L.log(f"[SKIP] 索引已存在 {INDEX_NAME}")
    else:
        body = {
            "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 512}},
            "mappings": {"properties": {
                VECTOR_FIELD: {
                    "type": "knn_vector", "dimension": 1024,
                    "method": {"name": "hnsw", "engine": "faiss",
                               "space_type": "l2",
                               "parameters": {"ef_construction": 512, "m": 16}},
                },
                TEXT_FIELD: {"type": "text"},
                META_FIELD: {"type": "text", "index": False},
            }},
        }
        # 重試建立（等 data access 生效）
        last = None
        for i in range(6):
            try:
                os_client.indices.create(index=INDEX_NAME, body=body)
                L.log(f"[OK] 建立向量索引 {INDEX_NAME}（1024 維 faiss hnsw）")
                last = None
                break
            except Exception as e:  # noqa
                last = str(e)[:200]
                L.log(f"  建立索引重試 {i+1}/6: {last}")
                time.sleep(15)
        if last:
            L.log(f"[FAIL] 索引建立失敗: {last}")
            L.log("STEP6 FAIL")
            return

    set_state("aoss_index_name", INDEX_NAME)
    set_state("aoss_vector_field", VECTOR_FIELD)
    set_state("aoss_text_field", TEXT_FIELD)
    set_state("aoss_meta_field", META_FIELD)
    # 索引建立後需短暫等待才能被 KB 使用
    time.sleep(30)
    L.log("STEP6 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
