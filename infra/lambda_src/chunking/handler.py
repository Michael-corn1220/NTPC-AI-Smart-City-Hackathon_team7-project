# -*- coding: utf-8 -*-
"""Bedrock Knowledge Base Custom Chunking Lambda。
契約（custom transformation）：
- 輸入 event 含 inputFiles[]，每個 file 的 contentBatches[] 指向 S3 上的中間 JSON（含 fileContents[]）。
- 我們讀取原始內容，依 Markdown 標題（#/##/###）切塊，讓「一條法條 = 一個 chunk」。
- 將切塊後的 fileContents 寫回 S3（同 bucket），並在回傳中指向新的 output S3 位置。

輸出格式須與輸入對齊：outputFiles[].contentBatches[].key 指向處理後 JSON。
"""
import json
import os
import re
import boto3

s3 = boto3.client("s3")

# 標題正則：# / ## / ###
HEADER_RE = re.compile(r"^(#{1,3})\s+(.*)$")


def split_markdown_by_headers(text):
    """依 Markdown 標題切塊，保留上層標題脈絡。回傳 [(chunk_text, header_path)]。"""
    lines = text.split("\n")
    chunks = []
    # 維護目前的標題階層路徑
    header_stack = {1: None, 2: None, 3: None}
    buf = []
    cur_path = []

    def flush():
        if buf and any(l.strip() for l in buf):
            body = "\n".join(buf).strip()
            if body:
                path = " > ".join([h for h in cur_path if h])
                # 把標題脈絡前置到 chunk，利於檢索與引用
                full = (path + "\n" + body) if path else body
                chunks.append((full, path))

    for line in lines:
        m = HEADER_RE.match(line.strip())
        if m:
            # 遇到新標題：先把前一段落 flush
            flush()
            buf = []
            level = len(m.group(1))
            title = m.group(2).strip()
            header_stack[level] = title
            # 清除更深層級
            for lv in range(level + 1, 4):
                header_stack[lv] = None
            cur_path = [header_stack[1], header_stack[2], header_stack[3]]
            cur_path = [h for h in cur_path if h]
            buf.append(line)  # 標題也保留在 chunk 內
        else:
            buf.append(line)
    flush()

    # 若整份沒有任何標題（例如純文字），退回原文單一 chunk
    if not chunks:
        stripped = text.strip()
        if stripped:
            chunks.append((stripped, ""))
    return chunks


# Titan Text Embeddings V2 上限 8192 token；中文 1 字元常 >=1 token，保守取 800 字元確保遠低於上限
MAX_CHARS = 800


def _split_long(text, max_chars=MAX_CHARS):
    """將過長文字依段落／句子再切成不超過 max_chars 的多塊，避免超過 embedding token 上限。"""
    if len(text) <= max_chars:
        return [text]
    parts = []
    buf = ""
    # 先依換行段落切，段落仍過長再依標點切
    units = []
    for para in text.split("\n"):
        if len(para) <= max_chars:
            units.append(para)
        else:
            seg = ""
            for ch in para:
                seg += ch
                if len(seg) >= max_chars and ch in "。！？；":
                    units.append(seg); seg = ""
            if seg:
                units.append(seg)
    for u in units:
        if len(buf) + len(u) + 1 > max_chars and buf:
            parts.append(buf); buf = u
        else:
            buf = (buf + "\n" + u) if buf else u
    if buf:
        parts.append(buf)
    # 極端情況（單一無標點長串）：硬切
    final = []
    for p in parts:
        if len(p) <= max_chars:
            final.append(p)
        else:
            for i in range(0, len(p), max_chars):
                final.append(p[i:i + max_chars])
    return final


def process_content(content_body):
    """content_body: 原始文字。回傳切塊後的 fileContents list（含長度保護）。"""
    results = []
    for chunk_text, path in split_markdown_by_headers(content_body):
        for piece in _split_long(chunk_text):
            results.append({
                "contentBody": piece,
                "contentType": "TEXT",
                "contentMetadata": {"header_path": path} if path else {},
            })
    return results


def handler(event, context):
    input_files = event.get("inputFiles", [])
    bucket = event.get("bucketName")
    output_files = []

    for f in input_files:
        original_location = f.get("originalFileLocation", {})
        file_metadata = f.get("fileMetadata", {})
        content_batches = f.get("contentBatches", [])
        processed_batches = []

        for batch in content_batches:
            key = batch.get("key")
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = json.loads(obj["Body"].read())
            in_contents = data.get("fileContents", [])

            new_contents = []
            for item in in_contents:
                body = item.get("contentBody", "")
                ctype = item.get("contentType", "TEXT")
                meta = item.get("contentMetadata", {})
                if ctype == "TEXT" and body:
                    for chunk in process_content(body):
                        merged_meta = dict(meta)
                        merged_meta.update(chunk.get("contentMetadata", {}))
                        new_contents.append({
                            "contentBody": chunk["contentBody"],
                            "contentType": "TEXT",
                            "contentMetadata": merged_meta,
                        })
                else:
                    new_contents.append(item)

            # 寫回處理後 JSON
            out_key = key.replace(".json", "") + "_chunked.json"
            s3.put_object(
                Bucket=bucket,
                Key=out_key,
                Body=json.dumps({"fileContents": new_contents}, ensure_ascii=False).encode("utf-8"),
            )
            processed_batches.append({"key": out_key})

        output_files.append({
            "originalFileLocation": original_location,
            "fileMetadata": file_metadata,
            "contentBatches": processed_batches,
        })

    return {"outputFiles": output_files}
