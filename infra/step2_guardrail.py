# -*- coding: utf-8 -*-
"""建立 Bedrock Guardrails（依規格第4節）。Idempotent。"""
from botocore.exceptions import ClientError
from common import client, slp, load_state, set_state, Logger, PREFIX

L = Logger("step2_result.txt")
NAME = f"{PREFIX}-guardrail"


def find_existing(bedrock):
    try:
        resp = bedrock.list_guardrails(maxResults=100)
        for g in resp.get("guardrails", []):
            if g.get("name") == NAME:
                return g.get("id")
    except ClientError as e:
        L.log(f"[WARN] list_guardrails: {e}")
    return None


def main():
    bedrock = client("bedrock")

    existing = find_existing(bedrock)
    if existing:
        L.log(f"[SKIP] Guardrail 已存在 {NAME} -> {existing}")
        set_state("guardrail_id", existing)
        L.log("STEP2 DONE")
        return
    slp()

    resp = bedrock.create_guardrail(
        name=NAME,
        description="新北市訴願 AI 系統防護：Grounding/Denied Topics/PII/Prompt Attack/中立視角",
        # Denied Topics：與案件無關之一般法律諮詢、對雙方之人格評價、裁決傾向性建議
        topicPolicyConfig={
            "topicsConfig": [
                {
                    "name": "unrelated_legal_advice",
                    "definition": "與本訴願案件無關之一般性法律諮詢或法律意見請求。",
                    "examples": [
                        "幫我看這份跟訴願無關的租賃契約有沒有問題",
                        "請給我一般的離婚訴訟建議",
                    ],
                    "type": "DENY",
                },
                {
                    "name": "personal_character_judgment",
                    "definition": "對訴願人、代理人或公務員之人格、可信度、動機做出結論性評價或人身攻擊。",
                    "examples": [
                        "這個訴願人明顯在說謊，不可信",
                        "承辦公務員很不專業",
                    ],
                    "type": "DENY",
                },
                {
                    "name": "adjudication_recommendation",
                    "definition": "在中立多元視角分析情境中，給出應如何裁決、傾向撤銷或駁回之結論性建議。",
                    "examples": [
                        "本案應該駁回",
                        "建議撤銷原處分",
                    ],
                    "type": "DENY",
                },
            ]
        },
        # Prompt Attack 防護
        contentPolicyConfig={
            "filtersConfig": [
                {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
                {"type": "INSULTS", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
                {"type": "HATE", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
            ]
        },
        # PII 過濾（作為 Comprehend 之後的最後防線）— 對輸出遮罩
        sensitiveInformationPolicyConfig={
            "piiEntitiesConfig": [
                {"type": "NAME", "action": "ANONYMIZE"},
                {"type": "ADDRESS", "action": "ANONYMIZE"},
                {"type": "PHONE", "action": "ANONYMIZE"},
                {"type": "EMAIL", "action": "ANONYMIZE"},
                {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"},
            ]
        },
        # Contextual Grounding：偵測草稿是否引用清單外法條
        contextualGroundingPolicyConfig={
            "filtersConfig": [
                {"type": "GROUNDING", "threshold": 0.75},
                {"type": "RELEVANCE", "threshold": 0.5},
            ]
        },
        blockedInputMessaging="您的請求超出本訴願輔助系統之服務範圍，已被安全防護攔截。",
        blockedOutputsMessaging="偵測到輸出內容不符合系統內容政策（可能引用未提供之法規或含不當評價），已攔截，請由承辦人員檢視。",
        tags=[{"key": "project", "value": PREFIX}],
    )
    gid = resp["guardrailId"]
    L.log(f"[OK] 建立 Guardrail {NAME} -> {gid} (version {resp.get('version')})")
    set_state("guardrail_id", gid)
    set_state("guardrail_arn", resp["guardrailArn"])
    slp()

    # 建立正式版本
    try:
        v = bedrock.create_guardrail_version(guardrailIdentifier=gid, description="v1 initial")
        L.log(f"[OK] Guardrail version -> {v.get('version')}")
        set_state("guardrail_version", v.get("version"))
    except ClientError as e:
        L.log(f"[WARN] create_guardrail_version: {e}")

    L.log("STEP2 DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa
        import traceback
        L.log("FATAL:\n" + traceback.format_exc())
    finally:
        L.flush()
