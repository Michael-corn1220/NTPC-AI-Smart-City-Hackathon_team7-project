# -*- coding: utf-8 -*-
"""訴願案件 pipeline Lambda（供 Step Functions 各階段呼叫）。
以 event['stage'] 分派到六階段之一。所有 Bedrock 呼叫走節流（此函式 reserved concurrency=1 + 呼叫前 sleep）確保 <=1 RPS。
本檔為黑客松展示骨架：邏輯完整、輸出符合規格 JSON 結構，僅使用合成/去識別化資料。
"""
import json
import os
import time
import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
GEN_MODEL = os.environ["GEN_MODEL_ID"]          # us.anthropic.claude-sonnet-4-5-...
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VER = os.environ.get("GUARDRAIL_VERSION", "1")
KB_ID = os.environ.get("KB_ID")
KB_SOURCE_BUCKET = os.environ.get("KB_SOURCE_BUCKET", "legal-dataset-hackathon-191149991440")
RERANK_ENDPOINT = os.environ.get("RERANK_ENDPOINT")
CASES_TABLE = os.environ["CASES_TABLE"]
DRAFTS_TABLE = os.environ["DRAFTS_TABLE"]

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
bedrock_agent_rt = boto3.client("bedrock-agent-runtime", region_name=REGION)
comprehend = boto3.client("comprehend", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
sm_runtime = boto3.client("sagemaker-runtime", region_name=REGION)

# 訴願法第 77 條八款法定不受理事由（供結構化擷取 System Prompt）
ARTICLE_77 = """一、訴願書不合法定程式不能補正或經通知補正逾期不補正者。
二、提起訴願逾法定期間或未於第五十七條但書所定期間內補送訴願書者。
三、訴願人不符合第十八條之規定者。
四、訴願人無訴願能力而未由法定代理人代為訴願行為，經通知補正逾期不補正者。
五、地方自治團體、法人、非法人之團體，未由代表人或管理人為訴願行為，經通知補正逾期不補正者。
六、行政處分已不存在者。
七、對已決定或已撤回之訴願事件重行提起訴願者。
八、對於非行政處分或其他依法不屬訴願救濟範圍內之事項提起訴願者。"""


def _throttle():
    """確保對 Bedrock 的請求 <=1 RPS（本函式 reserved concurrency=1，額外 sleep 保險）。"""
    time.sleep(1.1)


def call_claude(system_prompt, user_prompt, max_tokens=2000, apply_guardrail=True):
    """apply_guardrail=False 時不套用 Guardrail：
    用於多元視角分析等『輸入已去識別化、輸出為中立法學分析』的階段，
    避免 PII 遮罩把中文常用字（如『張』）誤判為人名遮成佔位，破壞 JSON 與語意。"""
    _throttle()
    kwargs = {
        "modelId": GEN_MODEL,
        "system": [{"text": system_prompt}],
        "messages": [{"role": "user", "content": [{"text": user_prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    if GUARDRAIL_ID and apply_guardrail:
        kwargs["guardrailConfig"] = {"guardrailIdentifier": GUARDRAIL_ID,
                                     "guardrailVersion": GUARDRAIL_VER}
    resp = bedrock.converse(**kwargs)
    return resp["output"]["message"]["content"][0]["text"]


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 修復嘗試 1：清掉 Guardrail PII 遮罩可能插入的佔位符（如 {NAME}、{位址} 等），避免污染字串值
    import re as _re
    repaired = _re.sub(r"\{[A-Z_]{2,}\}", "", text)  # 只清全大寫底線的佔位（PII 遮罩樣式）
    # 修復嘗試 2：擷取第一個 { 到最後一個 } 的區段
    l = repaired.find("{")
    r = repaired.rfind("}")
    if l >= 0 and r > l:
        cand = repaired[l:r + 1]
        try:
            return json.loads(cand)
        except Exception:
            pass
    # 修復嘗試 3：輸出被截斷（JSON 未收尾）→ 逐步補齊未閉合的括號/引號後再試
    frag = repaired[l:] if l >= 0 else repaired
    fixed = _try_close_json(frag)
    if fixed is not None:
        return fixed
    return {"_raw": text}


def _try_close_json(s):
    """嘗試修復被截斷的 JSON：移除尾端不完整片段，補齊未閉合的 ] 與 }。"""
    s = s.rstrip().rstrip(",")
    # 若最後一個字元在字串內（奇數個未跳脫引號），先截到最後一個完整的結構分界
    # 簡化策略：從尾端往前找最後一個 '}' 或 ']' 或 '"'，逐次嘗試補齊閉合符號
    for cut in range(len(s), max(0, len(s) - 400), -1):
        frag = s[:cut].rstrip().rstrip(",")
        # 計算需要補幾個 ] 與 }
        opens_brace = frag.count("{") - frag.count("}")
        opens_brack = frag.count("[") - frag.count("]")
        if opens_brace < 0 or opens_brack < 0:
            continue
        # 引號需成對；若為奇數，補一個收尾引號
        quote = frag.count('"') - frag.count('\\"')
        cand = frag
        if quote % 2 == 1:
            cand += '"'
        cand += "]" * opens_brack + "}" * opens_brace
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


# ---------- 各階段 ----------
def stage_deidentify(payload):
    text = payload.get("text", "")
    masked = text
    try:
        r = comprehend.detect_pii_entities(Text=text[:4900], LanguageCode="zh")
        # 依偵測結果由後往前遮罩，避免位移
        for ent in sorted(r.get("Entities", []), key=lambda e: e["BeginOffset"], reverse=True):
            b, e2 = ent["BeginOffset"], ent["EndOffset"]
            masked = masked[:b] + f"[{ent['Type']} 已遮罩]" + masked[e2:]
    except Exception as ex:  # noqa
        masked = text  # 展示環境資料已去識別化，Comprehend 失敗不致命
    return {"deidentified_text": masked}


def stage_extract(payload):
    system = ("你是新北市政府法制局的訴願案件助理，從去識別化後全文擷取結構化欄位。"
              "所有內容嚴格根據原文，未提及填 null，不得編造。僅回傳 JSON。\n"
              "判斷 procedural_dismissal_flag 時比對訴願法第77條下列八款：\n" + ARTICLE_77)
    user = (f"【訴願書全文】\n{payload.get('appellant_text','')}\n\n"
            f"【答辯書全文】\n{payload.get('agency_text','')}\n\n"
            '請輸出：{"case_type":"","dispute_type":"","procedural_dismissal_flag":false,'
            '"procedural_dismissal_reason":null,"appellant_points":[],"agency_points":[],'
            '"appeal_request":"","penalty_content":""}')
    return {"structured": parse_json(call_claude(system, user))}


def _kb_retrieve(query, prefix, k):
    """對 KB 檢索，並用 source-uri 前綴過濾（法條/ 或 相關判決書/）。"""
    if not KB_ID:
        return []
    _throttle()
    cfg = {"vectorSearchConfiguration": {"numberOfResults": k}}
    # 依 S3 來源 URI 前綴過濾，把「法條」與「判決書」分流
    if prefix:
        cfg["vectorSearchConfiguration"]["filter"] = {
            "startsWith": {
                "key": "x-amz-bedrock-kb-source-uri",
                "value": "s3://" + KB_SOURCE_BUCKET + "/" + prefix,
            }
        }
    try:
        r = bedrock_agent_rt.retrieve(knowledgeBaseId=KB_ID, retrievalQuery={"text": query},
                                      retrievalConfiguration=cfg)
        out = []
        for item in r.get("retrievalResults", []):
            out.append({
                "text": item.get("content", {}).get("text", ""),
                "score": item.get("score"),
                "location": item.get("location", {}),
            })
        return out
    except Exception as ex:  # noqa
        return [{"error": str(ex)[:200]}]


def _source_uri(item):
    loc = item.get("location", {}) or {}
    s3 = loc.get("s3Location", {}) or {}
    return s3.get("uri", "")


def _case_no_from_uri(uri):
    """從 S3 URI 取判決/決定書檔名作為案號顯示。"""
    if not uri:
        return "案例"
    name = uri.rstrip("/").split("/")[-1]
    for suf in (".md", ".pdf", " 的副本"):
        name = name.replace(suf, "")
    return name


def _rerank(query, docs):
    """用 SageMaker Cross-Encoder rerank 端點對候選判決書重排，回傳排序後索引與分數。"""
    if not RERANK_ENDPOINT or not docs:
        return None
    try:
        payload = {"query": query, "texts": [d.get("text", "")[:1500] for d in docs]}
        resp = sm_runtime.invoke_endpoint(
            EndpointName=RERANK_ENDPOINT, ContentType="application/json",
            Body=json.dumps(payload).encode("utf-8"))
        scores = json.loads(resp["Body"].read())
        # TEI reranker 回傳 [{"index":i,"score":s},...] 或 [s,...]
        if scores and isinstance(scores[0], dict):
            return [(s.get("index", i), s.get("score")) for i, s in enumerate(scores)]
        return sorted([(i, s) for i, s in enumerate(scores)], key=lambda x: x[1], reverse=True)
    except Exception:  # noqa
        return None


def _structure_regulations(regs):
    """用 Claude 把法條檢索片段整理成乾淨欄位：法規名稱+條號、要旨、狀態。僅依片段內容，不杜撰。"""
    if not regs:
        return []
    snippets = []
    for i, r in enumerate(regs[:12]):
        uri = _source_uri(r)
        law_hint = uri.rstrip("/").split("/")[-1].replace(".md", "").replace(".pdf", "").replace(" 的副本", "")
        snippets.append({"i": i, "law_file": law_hint, "text": r.get("text", "")[:600]})
    system = ("你是法規整理助手。根據檢索到的法規片段，整理出乾淨的法條清單。"
              "law_name 為法規名稱，article 為條號（如「第12條」，無法判斷填空字串），"
              "summary 為該片段的條文要旨（濃縮為一句，須忠於原文不得杜撰），"
              "status 一律填「現行有效」（除非片段明確指出已廢止或停止適用則填「需注意」）。"
              "去除重複條文，最多輸出 8 筆。僅回傳 JSON。")
    user = ("【法規片段（law_file 為來源檔名，可輔助判斷法規名稱）】\n"
            + json.dumps(snippets, ensure_ascii=False)
            + '\n\n請輸出：{"regulations":[{"law_name":"","article":"","summary":"","status":"現行有效","source_index":0}]}')
    parsed = parse_json(call_claude(system, user, max_tokens=1500))
    items = parsed.get("regulations", []) if isinstance(parsed, dict) else []
    # 回填來源 URI
    for it in items:
        si = it.get("source_index")
        if isinstance(si, int) and 0 <= si < len(regs):
            it["source_uri"] = _source_uri(regs[si])
    return items


def _ruling_from_name(case_no):
    """從檔名推斷主文結果（檔名常含結果字樣，如 '撤銷另處'、'訴願有理由'）。"""
    n = case_no or ""
    if "部分撤銷" in n:
        return "部分撤銷"
    if "撤銷" in n or "有理由" in n:
        return "撤銷發回"
    if "駁回" in n or "無理由" in n or "不受理" in n:
        return "駁回"
    return ""


def _ruling_from_text(text):
    """從片段文字推斷主文傾向。"""
    t = text or ""
    if "部分撤銷" in t:
        return "部分撤銷"
    if "訴願駁回" in t or "應予駁回" in t or "訴願為無理由" in t:
        return "駁回"
    if "原處分撤銷" in t or "原處分均撤銷" in t or "應予撤銷" in t or "訴願為有理由" in t:
        return "撤銷發回"
    return ""


def _structure_cases(similar):
    """把相似判決書整理出主文結果與論理重點（純規則，穩定不依賴額外 LLM 呼叫）。
    main_ruling 依案號字樣 + 片段文字推斷；reasoning 取片段重點文字。"""
    for s in similar:
        text = s.get("text", "") or ""
        ruling = _ruling_from_name(s.get("case_no", "")) or _ruling_from_text(text) or "未明"
        # 論理重點：取片段中較具論述性的一段（去除開頭雜訊，截斷至 130 字）
        reasoning = text.strip().replace("\n", " ")
        if len(reasoning) > 130:
            reasoning = reasoning[:130] + "…"
        s["main_ruling"] = ruling
        s["reasoning"] = reasoning or "（片段內容不足）"
    return similar


def stage_retrieve(payload):
    """分流檢索：法規比對（法條/函釋）與相似案例（判決書），後者以 rerank 重排取 Top 5。
    若本案為程序駁回（訴願法第77條），不進行法規比對與相似案例推薦。"""
    structured = payload.get("structured", {}) or {}
    if structured.get("procedural_dismissal_flag") is True:
        return {
            "regulation_matches": [], "regulation_list": [], "similar_cases": [],
            "skipped_reason": "本案符合訴願法第77條程序不受理要件，不進行法規比對與相似案例推薦",
        }
    query = payload.get("query", "")

    # 1. 法規比對：檢索全部「法條/」前綴，不依案件類別限定
    #    （已撤銷先前「依類別限縮檢索範圍以節省 token」之設計，改回跨全部法規檢索）
    regs = [x for x in _kb_retrieve(query, "法條/", 20) if "error" not in x]
    # 若過濾後為空，退回不限前綴檢索，避免完全無結果
    if not regs:
        regs = [x for x in _kb_retrieve(query, None, 20) if "error" not in x]

    # 2. 相似案例：只撈「相關判決書/」前綴，取 20 筆候選
    cases_raw = [x for x in _kb_retrieve(query, "相關判決書/", 20) if "error" not in x]

    # 3. rerank 重排取 Top 5
    ranked = _rerank(query, cases_raw)
    if ranked:
        top = ranked[:5]
        similar = []
        for idx, sc in top:
            if idx < len(cases_raw):
                item = cases_raw[idx]
                similar.append({
                    "case_no": _case_no_from_uri(_source_uri(item)),
                    "text": item.get("text", ""),
                    "score": sc if isinstance(sc, (int, float)) else item.get("score"),
                    "location": item.get("location", {}),
                    "source_uri": _source_uri(item),
                })
    else:
        # 無 rerank 時退回向量分數排序取前 5
        similar = []
        for item in cases_raw[:5]:
            similar.append({
                "case_no": _case_no_from_uri(_source_uri(item)),
                "text": item.get("text", ""),
                "score": item.get("score"),
                "location": item.get("location", {}),
                "source_uri": _source_uri(item),
            })

    # 4. 用 Claude 整理成結構化清單（法條：名稱/條號/要旨；案例：主文/論理重點）
    reg_structured = _structure_regulations(regs)
    similar = _structure_cases(similar)

    return {
        "regulation_matches": regs[:15],          # 原始片段（供草稿生成引用）
        "regulation_list": reg_structured,        # 結構化法條清單（供前端法規比對卡片）
        "similar_cases": similar,                 # 已含 main_ruling / reasoning
        "skipped_reason": "",
    }


def stage_draft(payload):
    structured = payload.get("structured", {}) or {}
    is_procedural = structured.get("procedural_dismissal_flag") is True

    if is_procedural:
        # 程序駁回（訴願不受理）：不寫事實欄，理由欄用公文慣例（按…引法條、查…敘程序事實並涵攝第77條各款）
        system = (
            "你是協助新北市政府法制局撰寫『程序不受理』訴願決定書草稿的助理，僅供承辦人員參考修改。"
            "本案經判斷符合訴願法第77條之程序不受理要件。請依下列規則撰寫，僅回傳 JSON：\n"
            "1. main_text 固定為「訴願不受理。」。\n"
            "2. facts_content 一律留空字串（程序駁回案件不撰寫事實欄）。\n"
            "3. reasons_content 依公文慣例撰寫，結構為：\n"
            "   (a) 先以「按」起頭，逐一引述所涉法條（訴願法第77條相關款次；如涉不合法定程式，"
            "併引第62條命補正、第57條補送訴願書等），涉及多條法條時以「次按」接續。\n"
            "   (b) 再以「查」起頭，敘明本案之程序要件事實（如訴願書未經簽名蓋章不合法定程式、"
            "提起訴願逾30日法定期間、對非行政處分提起訴願等）。\n"
            "   (c) 進行簡要涵攝，說明本案確實符合訴願法第77條第幾款之情形。\n"
            "   (d) 若屬『得補正』之情形（例如訴願書未簽名蓋章等程式欠缺可補正者），"
            "應敘明依訴願法第62條應通知訴願人於20日內補正，逾期不補正始不受理；"
            "若屬不能補正或逾期未補正，則敘明其結論。\n"
            "4. notice_content 為教示規定（不受理決定之救濟教示）。\n"
            "用語須符合公文慣例（按、次按、查、又、綜上等）。不得杜撰清單外之法條。"
        )
        user = (
            f"【案件結構化資料（含 procedural_dismissal_reason 命中之款次）】\n"
            f"{json.dumps(structured, ensure_ascii=False)}\n\n"
            f"【可引用之法規與函釋清單（僅能引用此清單，若清單為空則依訴願法第77條、第62條等一般程序規定論述）】\n"
            f"{json.dumps(payload.get('regulation_matches', []), ensure_ascii=False)}\n\n"
            '請輸出：{"main_text":"訴願不受理。","facts_content":"","reasons_content":"","notice_content":""}'
        )
        return {"draft": parse_json(call_claude(system, user, max_tokens=2500))}

    # 實體案件：完整四欄
    system = ("你是協助新北市政府法制局撰寫訴願決定書草稿的助理，草稿僅供承辦人員參考修改。"
              "只能引用【已提供的法規與函釋清單】中的法條，清單無適用法條時於理由欄註明"
              "「尚待承辦人員補充法規依據」。事實欄僅陳述雙方已提出事實。理由欄用語符合公文慣例"
              "（按、查、惟、故等），涉及多條法條以「次按」接續。僅回傳 JSON。")
    user = (f"【案件結構化資料】\n{json.dumps(structured,ensure_ascii=False)}\n\n"
            f"【已提供的法規與函釋清單】\n{json.dumps(payload.get('regulation_matches',[]),ensure_ascii=False)}\n\n"
            '請輸出：{"main_text":"","facts_content":"","reasons_content":"","notice_content":""}')
    return {"draft": parse_json(call_claude(system, user, max_tokens=3000))}


def stage_revise(payload):
    """草稿指令式修訂：依承辦人員自然語言指令修改指定欄位，不得擅自變更主文實質結論。"""
    current = payload.get("current_draft", {})
    instruction = payload.get("instruction", "")
    target = payload.get("target", "reasons")
    field_map = {"main": "main_text", "facts": "facts_content",
                 "reasons": "reasons_content", "notice": "notice_content"}
    system = ("你是訴願決定書草稿修訂助理。承辦人員會針對草稿特定欄位下修改指令，"
              "你只修改該指令所指涉的欄位，不得更動其他欄位，也不得變更主文之實質結論"
              "（除非指令明確要求）。若指令要求變更實質結論，於 warning 標註需人工確認。"
              "僅回傳 JSON。")
    user = ("【目前草稿】\n" + json.dumps(current, ensure_ascii=False)
            + "\n\n【承辦人員指令】\n" + instruction
            + "\n\n【指令目標欄位】" + target
            + '\n\n請輸出：{"target_field":"' + target + '","updated_content":"（修訂後的該欄位完整內容）","warning":null}')
    parsed = parse_json(call_claude(system, user, max_tokens=3000))
    revised = dict(current)
    tf = field_map.get(target, "reasons_content")
    if isinstance(parsed, dict) and parsed.get("updated_content"):
        revised[tf] = parsed["updated_content"]
    return {"revised_draft": revised, "target_field": target,
            "warning": parsed.get("warning") if isinstance(parsed, dict) else None}


def _clip_points(points, max_chars):
    """把論點清單序列化為 JSON 字串，並限制總長度上限（避免文件過長使 prompt 過大、生成超時）。
    超過上限時逐項納入直到接近上限，確保『不管原始文件多長，送進模型的輸入都被控制在安全範圍』。"""
    if not isinstance(points, list):
        points = [points] if points else []
    kept = []
    total = 0
    for p in points:
        s = p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
        if total + len(s) > max_chars:
            # 若第一項就超長，仍納入截斷後的片段，保底有內容
            if not kept:
                kept.append(s[:max_chars])
            break
        kept.append(s)
        total += len(s)
    return json.dumps(kept, ensure_ascii=False)


def stage_perspective(payload):
    """中立多元視角分析（核心）：雙方摘要、爭點、值得留意的細節。控制輸出量以免 API Gateway 逾時。"""
    # 輸入端截斷：無論來源文件多長，雙方論點各限約 2800 字，確保 prompt 大小穩定、單次生成穩定在 29 秒內
    appellant = _clip_points(payload.get("appellant_points", []), 2800)
    agency = _clip_points(payload.get("agency_points", []), 2800)
    system = (
        "你是新北市政府法制局的中立多元視角分析助理。針對雙方主張提供不預設立場、對等篇幅之補充分析。"
        "用語中性、不評價可信度、不給裁決建議；內容須可回溯雙方主張。僅回傳 JSON，陣列勿留空。"
    )
    user = (
        f"【訴願人主張】\n{appellant}\n\n【原處分機關答辯】\n{agency}\n\n"
        "請輸出 JSON（key_disputes 2 項、notable_details 2 項；各欄務必精簡，每項一句話，避免冗長）：\n"
        "{"
        '"appellant_summary":"中性摘要訴願人核心主張（2-3句）",'
        '"agency_summary":"中性摘要機關核心答辯，篇幅相當（2-3句）",'
        '"key_disputes":[{"dispute":"爭點一句話","appellant_view":"訴願人立場","agency_view":"機關立場"}],'
        '"notable_details":[{"detail":"值得留意的細節","related_to":"訴願人主張|機關答辯|雙方共通","source_reference":"對應原文或註明原文未載明"}],'
        '"disclaimer":"本分析為中立參考，不代表任何一方立場，亦不構成決定書之直接論據，實質判斷仍應由承辦人員依卷證資料自行認定"'
        "}"
    )
    # max_tokens=1500：配合輸出結構收斂（爭點/細節各2項），確保 JSON 能完整輸出不被截斷，
    # 同時仍穩定在 API Gateway 29 秒上限內（實測複雜案件約 13 秒）。
    # apply_guardrail=False：此階段輸入已去識別化，關閉 Guardrail 避免中文字被誤遮破壞 JSON。
    pv = parse_json(call_claude(system, user, max_tokens=1500, apply_guardrail=False))
    return {"perspective": pv}


def _pv_inputs(payload):
    # 延伸階段輸入同樣截斷（各限約 2500 字），確保單次呼叫穩定在 API Gateway 29 秒上限內
    return (_clip_points(payload.get("appellant_points", []), 2500),
            _clip_points(payload.get("agency_points", []), 2500))


def stage_perspective_angles(payload):
    """延伸-多視角提問（單一類別，快速）。"""
    a, g = _pv_inputs(payload)
    system = ("你是中立多元視角分析助理。針對雙方爭點，從不同法學視角提出應追問的關鍵問題。中性、不給裁決建議。僅回傳 JSON。")
    user = (f"【訴願人】{a}\n【機關】{g}\n\n請輸出（3-4 項，每項一句）："
            '{"multi_perspectives":[{"angle":"視角名稱(如程序保障/證據法則/比例原則)","question":"關鍵問題"}]}')
    mp = parse_json(call_claude(system, user, max_tokens=900, apply_guardrail=False)).get("multi_perspectives", [])
    return {"multi_perspectives": mp}


def stage_perspective_court(payload):
    """延伸-法院實務一般見解（單一類別，快速）。"""
    a, g = _pv_inputs(payload)
    system = ("你是法學整理助理。就本案爭點，整理法院實務之一般見解（通識層次，非特定判決，不得杜撰案號）。"
              "所有法院實務一般見解需確定是真實存在、法院確實曾提出之見解，不得由 AI 自行推論或杜撰；"
              "每項見解須標示可回溯之來源或提出者（如法院層級、實務通說出處），無法標示可靠來源者則不列出；"
              "若無把握為真實見解則不列出。僅回傳 JSON。")
    user = (f"【訴願人】{a}\n【機關】{g}\n\n請輸出（3 項，每項一句，並標示來源或提出者）："
            '{"court_views":[{"view":"法院實務一般見解","source":"來源或提出者(如最高行政法院通說/司法實務見解出處)"}]}')
    views = parse_json(call_claude(system, user, max_tokens=900, apply_guardrail=False)).get("court_views", [])
    # 相容處理：模型可能回字串或物件，統一成 {view, source}
    norm = []
    for v in views:
        if isinstance(v, dict):
            norm.append({"view": v.get("view", ""), "source": v.get("source", "")})
        else:
            norm.append({"view": str(v), "source": ""})
    return {"court_views": norm}


def stage_perspective_chat(payload):
    """多元視角討論：承辦人員針對分析內容追問，AI 依卷內資料與一般法學通識回覆。"""
    question = payload.get("question", "")
    context = payload.get("context", {})  # 前次分析結果
    history = payload.get("history", [])  # [{role, text}]
    system = (
        "你是新北市政府法制局的中立多元視角討論助理。承辦人員會針對先前的中立分析內容追問。"
        "回覆須：依卷內雙方主張與一般法學通識作答；卷內未載明的事實明確告知不臆測；"
        "保持中性、不給應如何裁決之結論；可提出值得思考的方向與不同視角。僅回傳 JSON。"
    )
    hist_txt = "\n".join([("承辦人員：" if h.get("role") == "user" else "AI：") + h.get("text", "")
                          for h in history[-6:]])
    user = (
        f"【先前中立分析摘要】\n{json.dumps(context, ensure_ascii=False)[:2500]}\n\n"
        f"【對話紀錄】\n{hist_txt}\n\n【承辦人員本次提問】\n{question}\n\n"
        '請輸出：{"answer":"你的中立回覆（一段文字）"}'
    )
    parsed = parse_json(call_claude(system, user, max_tokens=1500, apply_guardrail=False))
    ans = parsed.get("answer") if isinstance(parsed, dict) else None
    if not ans:
        ans = parsed.get("_raw", "（無法產生回覆，請換個問法）") if isinstance(parsed, dict) else "（無法產生回覆）"
    return {"answer": ans}


def stage_finalize(payload):
    """把六階段結果整合寫回 DynamoDB cases 表與 drafts 表，供 result 端點查詢。"""
    import uuid
    from decimal import Decimal
    case_id = payload.get("case_id")
    structured = payload.get("structured", {})
    regulation_matches = payload.get("regulation_matches", [])
    draft = payload.get("draft", {})
    result_obj = {
        "structured": structured,
        "regulation_matches": regulation_matches,
        "regulation_list": payload.get("regulation_list", []),
        "similar_cases": payload.get("similar_cases", []),
        "procedural_skip": payload.get("procedural_skip", ""),
        "draft": draft,
    }
    draft_id = f"d_{uuid.uuid4().hex[:12]}"
    if case_id:
        # cases 表：寫入結果與狀態
        cases = ddb.Table(CASES_TABLE)
        cases.update_item(
            Key={"case_id": case_id},
            UpdateExpression="SET #s=:s, result_json=:r, draft_id=:d",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":r": json.dumps(result_obj, ensure_ascii=False),
                ":d": draft_id,
            },
        )
        # drafts 表：寫入草稿版本
        try:
            drafts = ddb.Table(DRAFTS_TABLE)
            drafts.put_item(Item={
                "draft_id": draft_id, "case_id": case_id,
                "main_text": draft.get("main_text", ""),
                "facts_content": draft.get("facts_content", ""),
                "reasons_content": draft.get("reasons_content", ""),
                "notice_content": draft.get("notice_content", ""),
                "version": Decimal("1"),
                "updated_at": Decimal(str(int(time.time()))),
            })
        except Exception:  # noqa
            pass
    return {"case_id": case_id, "draft_id": draft_id, "status": "completed"}


def stage_classify(payload):
    """輕量案件類別辨識：上傳檔案後即時判斷案件類別（單一分類，快速）。"""
    text = (payload.get("text", "") or "")[:6000]
    system = ("你是案件分類助手。判斷這份訴願／答辯文件所屬案件類別，"
              "只能從『廢棄物清理法』『洗錢防制法』『空氣污染防制法』『其他』擇一。僅回傳 JSON。")
    user = "【文件內容】\n" + text + '\n\n請輸出：{"category":"廢棄物清理法|洗錢防制法|空氣污染防制法|其他"}'
    parsed = parse_json(call_claude(system, user, max_tokens=200))
    cat = parsed.get("category") if isinstance(parsed, dict) else None
    if cat not in ("廢棄物清理法", "洗錢防制法", "空氣污染防制法", "其他"):
        cat = "其他"
    return {"category": cat}


STAGES = {
    "classify": stage_classify,
    "deidentify": stage_deidentify,
    "extract": stage_extract,
    "retrieve": stage_retrieve,
    "draft": stage_draft,
    "perspective": stage_perspective,
    "perspective_angles": stage_perspective_angles,
    "perspective_court": stage_perspective_court,
    "perspective_chat": stage_perspective_chat,
    "revise": stage_revise,
    "finalize": stage_finalize,
}


def handler(event, context):
    stage = event.get("stage")
    payload = event.get("payload", event)
    fn = STAGES.get(stage)
    if not fn:
        return {"error": f"unknown stage: {stage}", "available": list(STAGES)}
    result = fn(payload)
    return {"stage": stage, "result": result}
