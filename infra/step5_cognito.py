# -*- coding: utf-8 -*-
"""建立 Cognito User Pool（單一角色示意）+ App Client。Idempotent。"""
from botocore.exceptions import ClientError
from common import client, slp, load_state, set_state, Logger, PREFIX

L = Logger("step5_result.txt")
POOL_NAME = f"{PREFIX}-users"


def main():
    cog = client("cognito-idp")

    # 檢查是否已存在
    existing_id = None
    try:
        resp = cog.list_user_pools(MaxResults=60)
        for p in resp.get("UserPools", []):
            if p.get("Name") == POOL_NAME:
                existing_id = p.get("Id")
    except ClientError as e:
        L.log(f"[WARN] list_user_pools: {e}")
    slp()

    if existing_id:
        L.log(f"[SKIP] User Pool 已存在 {POOL_NAME} -> {existing_id}")
        pool_id = existing_id
    else:
        resp = cog.create_user_pool(
            PoolName=POOL_NAME,
            Policies={"PasswordPolicy": {
                "MinimumLength": 8, "RequireUppercase": True, "RequireLowercase": True,
                "RequireNumbers": True, "RequireSymbols": False,
            }},
            AutoVerifiedAttributes=["email"],
            UsernameAttributes=["email"],
            MfaConfiguration="OFF",
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            UserPoolTags={"project": PREFIX},
        )
        pool_id = resp["UserPool"]["Id"]
        L.log(f"[OK] 建立 User Pool {POOL_NAME} -> {pool_id}")
        slp()

    set_state("cognito_user_pool_id", pool_id)

    # App Client
    client_id = None
    try:
        lc = cog.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)
        for c in lc.get("UserPoolClients", []):
            if c.get("ClientName") == f"{PREFIX}-web":
                client_id = c.get("ClientId")
    except ClientError as e:
        L.log(f"[WARN] list_user_pool_clients: {e}")
    slp()

    if client_id:
        L.log(f"[SKIP] App Client 已存在 -> {client_id}")
    else:
        rc = cog.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=f"{PREFIX}-web",
            GenerateSecret=False,  # 前端 SPA，不用 secret
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH",
                               "ALLOW_REFRESH_TOKEN_AUTH"],
            PreventUserExistenceErrors="ENABLED",
        )
        client_id = rc["UserPoolClient"]["ClientId"]
        L.log(f"[OK] 建立 App Client {PREFIX}-web -> {client_id}")
        slp()

    set_state("cognito_app_client_id", client_id)
    L.log("STEP5 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
