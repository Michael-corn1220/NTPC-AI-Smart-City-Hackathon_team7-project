# -*- coding: utf-8 -*-
"""step10 前端對外部署（競賽 demo 定位）：
1. 建前端託管 S3 bucket（Block Public Access 全開、私有）
2. 上傳 web/index.html
3. 建 CloudFront OAC + distribution（HTTPS、預設根物件 index.html）
4. 設 bucket policy 只允許該 distribution 存取（OAC，SourceArn 綁 distribution）
5. 在 us-east-1 建 WAF WebACL（IPSet 4 組 IP + 常見攻擊 managed rules + 速率限制），綁 CloudFront
6. 後端 API Gateway 加 resource policy 限同 4 組 IP，重新部署 stage
所有結果寫回 state.json，log 寫檔。
"""
import json
import os
import time
import boto3
from botocore.config import Config
from common import load_state, set_state, Logger

ACCOUNT = "191149991440"
PREFIX = "ntpc-appeals"
REGION_W = "us-west-2"
ALLOW_IPS = ["60.250.71.45", "61.222.117.53", "59.125.121.41", "60.250.71.43"]
ALLOW_CIDRS = [ip + "/32" for ip in ALLOW_IPS]

cfg_w = Config(region_name=REGION_W, retries={"max_attempts": 5, "mode": "standard"})
cfg_e = Config(region_name="us-east-1", retries={"max_attempts": 5, "mode": "standard"})

s3 = boto3.client("s3", config=cfg_w)
cf = boto3.client("cloudfront", config=cfg_e)   # CloudFront 全球，SDK 走 us-east-1
wafe = boto3.client("wafv2", config=cfg_e)      # CLOUDFRONT scope 必須 us-east-1
apigw = boto3.client("apigateway", config=cfg_w)

L = Logger("step10_deploy_result.txt")
st = load_state()
WEB_BUCKET = f"{PREFIX}-web-{ACCOUNT}"
STAGE = "v1"


def create_web_bucket():
    try:
        s3.create_bucket(Bucket=WEB_BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": REGION_W})
        L.log(f"[OK] 建立前端 bucket {WEB_BUCKET}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        L.log(f"[SKIP] 前端 bucket 已存在 {WEB_BUCKET}")
    except Exception as e:
        if "BucketAlreadyOwnedByYou" in str(e) or "BucketAlreadyExists" in str(e):
            L.log(f"[SKIP] 前端 bucket 已存在 {WEB_BUCKET}")
        else:
            raise
    # Block Public Access 全開（私有，只給 CloudFront OAC）
    s3.put_public_access_block(
        Bucket=WEB_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    L.log("[OK] Block Public Access 全開")
    set_state("web_bucket", WEB_BUCKET)


def upload_index():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "index.html")
    with open(path, "rb") as f:
        body = f.read()
    s3.put_object(Bucket=WEB_BUCKET, Key="index.html", Body=body,
                  ContentType="text/html; charset=utf-8", CacheControl="no-cache")
    L.log(f"[OK] 上傳 index.html（{len(body)} bytes）")


def ensure_oac():
    name = f"{PREFIX}-web-oac"
    # 找既有 OAC
    lst = cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", [])
    for it in lst:
        if it.get("Name") == name:
            L.log(f"[SKIP] OAC 已存在 {it['Id']}")
            return it["Id"]
    r = cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": name, "Description": "OAC for ntpc-appeals web",
        "SigningProtocol": "sigv4", "OriginAccessControlOriginType": "s3",
        "SigningBehavior": "always"})
    oac_id = r["OriginAccessControl"]["Id"]
    L.log(f"[OK] 建立 OAC {oac_id}")
    return oac_id


def create_distribution(oac_id, waf_arn=None):
    # 若 state 已有 distribution，略過
    existing = st.get("cloudfront_distribution_id")
    if existing:
        L.log(f"[SKIP] CloudFront distribution 已存在 {existing}")
        return existing, st.get("cloudfront_domain")
    origin_domain = f"{WEB_BUCKET}.s3.{REGION_W}.amazonaws.com"
    cfg = {
        "CallerReference": f"{PREFIX}-web-{int(time.time())}",
        "Comment": "ntpc-appeals web (hackathon demo)",
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "s3-web",
            "DomainName": origin_domain,
            "OriginAccessControlId": oac_id,
            "S3OriginConfig": {"OriginAccessIdentity": ""},
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "s3-web",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "Compress": True,
            # CachingOptimized managed policy id
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
        },
        "PriceClass": "PriceClass_All",
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
    }
    if waf_arn:
        cfg["WebACLId"] = waf_arn
    r = cf.create_distribution(DistributionConfig=cfg)
    dist = r["Distribution"]
    dist_id = dist["Id"]
    domain = dist["DomainName"]
    arn = dist["ARN"]
    L.log(f"[OK] 建立 CloudFront distribution {dist_id} domain={domain}")
    set_state("cloudfront_distribution_id", dist_id)
    set_state("cloudfront_domain", domain)
    set_state("cloudfront_arn", arn)
    return dist_id, domain


def set_bucket_policy(dist_arn):
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowCloudFrontOAC",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{WEB_BUCKET}/*",
            "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}},
        }],
    }
    s3.put_bucket_policy(Bucket=WEB_BUCKET, Policy=json.dumps(policy))
    L.log("[OK] 設定 bucket policy（只允許此 CloudFront distribution OAC 存取）")


def main():
    L.log("===== step10 前端對外部署 =====")
    create_web_bucket()
    upload_index()
    oac_id = ensure_oac()
    set_state("web_oac_id", oac_id)
    # 先不綁 WAF 建 distribution（WAF 在 step10b 建好後再 update），避免相依卡住
    dist_id, domain = create_distribution(oac_id)
    # 取 distribution ARN 設 bucket policy
    s2 = load_state()
    set_bucket_policy(s2["cloudfront_arn"])
    L.log("")
    L.log(f"CloudFront domain: https://{domain}")
    L.log("STEP10 DEPLOY DONE")
    L.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
        L.flush()
