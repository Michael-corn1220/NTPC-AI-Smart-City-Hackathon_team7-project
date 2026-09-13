# -*- coding: utf-8 -*-
"""step10b：在 us-east-1 建 WAF WebACL（CLOUDFRONT scope）並綁定 CloudFront distribution。
- IPSet：4 組允許 IP
- 規則：AWS 常見攻擊 managed rule group + 速率限制 + IP 允許清單
- 初始建為 restricted 模式（DefaultAction=Block，只放行 IPSet）
切換由 step10_access_mode.py 負責。
"""
import json
import time
import boto3
from botocore.config import Config
from common import load_state, set_state, Logger

PREFIX = "ntpc-appeals"
ALLOW_CIDRS = ["60.250.71.45/32", "61.222.117.53/32", "59.125.121.41/32", "60.250.71.43/32"]

cfg_e = Config(region_name="us-east-1", retries={"max_attempts": 5, "mode": "standard"})
waf = boto3.client("wafv2", config=cfg_e)
cf = boto3.client("cloudfront", config=cfg_e)

L = Logger("step10b_waf_result.txt")
st = load_state()
IPSET_NAME = f"{PREFIX}-allow-ips"
ACL_NAME = f"{PREFIX}-web-acl"


def ensure_ipset():
    lst = waf.list_ip_sets(Scope="CLOUDFRONT", Limit=100).get("IPSets", [])
    for it in lst:
        if it["Name"] == IPSET_NAME:
            L.log(f"[SKIP] IPSet 已存在 {it['Id']}")
            return it["Id"], it["ARN"]
    r = waf.create_ip_set(Name=IPSET_NAME, Scope="CLOUDFRONT", IPAddressVersion="IPV4",
                          Addresses=ALLOW_CIDRS, Description="ntpc-appeals allowed client IPs")
    s = r["Summary"]
    L.log(f"[OK] 建立 IPSet {s['Id']}")
    return s["Id"], s["ARN"]


def build_rules(ipset_arn):
    """規則優先序：
    1) 速率限制（任何來源，2000/5min，超過即擋）— 防 DDoS/暴力
    2) AWS Managed Common Rule Set — 擋常見攻擊
    3) AWS Managed Known Bad Inputs
    4) 允許清單 IP（Allow）— restricted 模式下配合 DefaultAction=Block 只放這些 IP
    """
    return [
        {
            "Name": "RateLimit", "Priority": 0,
            "Statement": {"RateBasedStatement": {"Limit": 2000, "AggregateKeyType": "IP"}},
            "Action": {"Block": {}},
            "VisibilityConfig": {"SampledRequestsEnabled": True,
                                 "CloudWatchMetricsEnabled": True, "MetricName": "RateLimit"},
        },
        {
            "Name": "AWSCommonRules", "Priority": 1,
            "OverrideAction": {"None": {}},
            "Statement": {"ManagedRuleGroupStatement": {
                "VendorName": "AWS", "Name": "AWSManagedRulesCommonRuleSet"}},
            "VisibilityConfig": {"SampledRequestsEnabled": True,
                                 "CloudWatchMetricsEnabled": True, "MetricName": "AWSCommonRules"},
        },
        {
            "Name": "AWSBadInputs", "Priority": 2,
            "OverrideAction": {"None": {}},
            "Statement": {"ManagedRuleGroupStatement": {
                "VendorName": "AWS", "Name": "AWSManagedRulesKnownBadInputsRuleSet"}},
            "VisibilityConfig": {"SampledRequestsEnabled": True,
                                 "CloudWatchMetricsEnabled": True, "MetricName": "AWSBadInputs"},
        },
        {
            "Name": "AllowListedIPs", "Priority": 3,
            "Statement": {"IPSetReferenceStatement": {"ARN": ipset_arn}},
            "Action": {"Allow": {}},
            "VisibilityConfig": {"SampledRequestsEnabled": True,
                                 "CloudWatchMetricsEnabled": True, "MetricName": "AllowListedIPs"},
        },
    ]


def ensure_web_acl(ipset_arn):
    existing = st.get("waf_web_acl_id")
    if existing:
        L.log(f"[SKIP] WebACL 已存在 {existing}")
        return existing, st.get("waf_web_acl_arn")
    # 初始 restricted：DefaultAction=Block（只放行 AllowListedIPs 規則命中的來源）
    r = waf.create_web_acl(
        Name=ACL_NAME, Scope="CLOUDFRONT",
        DefaultAction={"Block": {}},
        Description="ntpc-appeals web access control restricted default",
        Rules=build_rules(ipset_arn),
        VisibilityConfig={"SampledRequestsEnabled": True,
                          "CloudWatchMetricsEnabled": True, "MetricName": ACL_NAME})
    s = r["Summary"]
    L.log(f"[OK] 建立 WebACL {s['Id']}（初始 restricted：預設 Block，只放 4 組 IP）")
    set_state("waf_web_acl_id", s["Id"])
    set_state("waf_web_acl_arn", s["ARN"])
    set_state("waf_web_acl_name", ACL_NAME)
    return s["Id"], s["ARN"]


def attach_to_cloudfront(acl_arn):
    dist_id = st["cloudfront_distribution_id"]
    # 取現有 config，更新 WebACLId，再 update
    resp = cf.get_distribution_config(Id=dist_id)
    etag = resp["ETag"]
    dcfg = resp["DistributionConfig"]
    if dcfg.get("WebACLId") == acl_arn:
        L.log("[SKIP] CloudFront 已綁定此 WebACL")
        return
    dcfg["WebACLId"] = acl_arn
    cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=dcfg)
    L.log(f"[OK] CloudFront {dist_id} 綁定 WebACL")


def main():
    L.log("===== step10b WAF (us-east-1, CLOUDFRONT scope) =====")
    ipset_id, ipset_arn = ensure_ipset()
    set_state("waf_ipset_id", ipset_id)
    set_state("waf_ipset_arn", ipset_arn)
    set_state("waf_ipset_name", IPSET_NAME)
    acl_id, acl_arn = ensure_web_acl(ipset_arn)
    attach_to_cloudfront(acl_arn)
    L.log("")
    L.log("STEP10B WAF DONE（目前為 restricted 模式）")
    L.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
        L.flush()
