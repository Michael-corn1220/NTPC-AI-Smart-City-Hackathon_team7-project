# -*- coding: utf-8 -*-
"""存取模式切換：一次切「前端 CloudFront WAF 預設行為」+「後端 API Gateway resource policy」。

用法：
  python step10_access_mode.py restricted   # 只允許 4 組 IP（前端 WAF 預設 Block 只放 IPSet；後端 API deny 非允許 IP）
  python step10_access_mode.py open          # 完全開放（前端 WAF 預設 Allow；後端 API 全允許）
  python step10_access_mode.py status        # 查目前模式

restricted 與 open 都保留 WAF 的速率限制與 AWS 常見攻擊 managed rules（僅切換「預設是否放行」與「IP 允許清單」是否作為唯一入口）。
"""
import json
import sys
import time
import boto3
from botocore.config import Config
from common import load_state, set_state, Logger

REGION_W = "us-west-2"
ACCOUNT = "191149991440"
ALLOW_CIDRS = ["60.250.71.45/32", "61.222.117.53/32", "59.125.121.41/32", "60.250.71.43/32"]
STAGE = "v1"

cfg_w = Config(region_name=REGION_W, retries={"max_attempts": 5, "mode": "standard"})
cfg_e = Config(region_name="us-east-1", retries={"max_attempts": 5, "mode": "standard"})
waf = boto3.client("wafv2", config=cfg_e)
apigw = boto3.client("apigateway", config=cfg_w)
L = Logger("step10_access_mode_result.txt")
st = load_state()
ACL_NAME = st.get("waf_web_acl_name", "ntpc-appeals-web-acl")
ACL_ID = st["waf_web_acl_id"]
API_ID = st["api_id"]


# ---------- 前端 WAF ----------
def set_waf_default(mode):
    """restricted → DefaultAction=Block（只放 AllowListedIPs 規則命中者）
       open       → DefaultAction=Allow（任何 IP 皆放行；仍受 rate limit 與 managed rules 檢查）"""
    r = waf.get_web_acl(Name=ACL_NAME, Scope="CLOUDFRONT", Id=ACL_ID)
    acl = r["WebACL"]
    lock = r["LockToken"]
    default_action = {"Block": {}} if mode == "restricted" else {"Allow": {}}
    waf.update_web_acl(
        Name=ACL_NAME, Scope="CLOUDFRONT", Id=ACL_ID, LockToken=lock,
        DefaultAction=default_action,
        Rules=acl["Rules"],
        VisibilityConfig=acl["VisibilityConfig"],
        Description=acl.get("Description", "ntpc-appeals web access control"))
    L.log(f"[OK] 前端 WAF DefaultAction -> {'Block(只放4組IP)' if mode=='restricted' else 'Allow(完全開放)'}")


# ---------- 後端 API resource policy ----------
def _resource_arn():
    return f"arn:aws:execute-api:{REGION_W}:{ACCOUNT}:{API_ID}/*"


def set_api_policy(mode):
    resource = _resource_arn()
    if mode == "restricted":
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": "*", "Action": "execute-api:Invoke",
                 "Resource": resource},
                {"Effect": "Deny", "Principal": "*", "Action": "execute-api:Invoke",
                 "Resource": resource,
                 "Condition": {"NotIpAddress": {"aws:SourceIp": ALLOW_CIDRS}}},
            ],
        }
    else:  # open
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": "*", "Action": "execute-api:Invoke",
                 "Resource": resource},
            ],
        }
    apigw.update_rest_api(
        restApiId=API_ID,
        patchOperations=[{"op": "replace", "path": "/policy", "value": json.dumps(policy)}])
    time.sleep(2)
    apigw.create_deployment(restApiId=API_ID, stageName=STAGE,
                            description=f"access mode: {mode}")
    L.log(f"[OK] 後端 API resource policy -> {mode}，已重新部署 stage {STAGE}")


def show_status():
    # WAF 預設行為
    r = waf.get_web_acl(Name=ACL_NAME, Scope="CLOUDFRONT", Id=ACL_ID)
    da = r["WebACL"]["DefaultAction"]
    waf_mode = "restricted(Block)" if "Block" in da else "open(Allow)"
    # API policy
    api = apigw.get_rest_api(restApiId=API_ID)
    pol = api.get("policy")
    api_mode = "unknown"
    if pol:
        # policy 會被 escape，判斷是否含 Deny NotIpAddress
        api_mode = "restricted(deny non-allowlist)" if "NotIpAddress" in pol else "open(allow all)"
    else:
        api_mode = "open(no policy)"
    L.log(f"前端 WAF: {waf_mode}")
    L.log(f"後端 API: {api_mode}")
    L.log(f"目前記錄模式（state）: {st.get('access_mode', '未記錄')}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    L.log(f"===== access mode: {mode} =====")
    if mode == "status":
        show_status()
    elif mode in ("restricted", "open"):
        set_waf_default(mode)
        set_api_policy(mode)
        set_state("access_mode", mode)
        L.log("")
        L.log(f"切換完成 -> {mode}")
        L.log("注意：CloudFront 設定傳播與 WAF 生效約需數十秒至數分鐘。")
    else:
        L.log(f"未知模式：{mode}。可用：restricted | open | status")
    L.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
        L.flush()
