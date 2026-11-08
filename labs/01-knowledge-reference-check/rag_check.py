"""RAG 参照確認スクリプト。

使い方:
  python3 rag_check.py index [資料フォルダ]          # 既定: corpus
  python3 rag_check.py ask "質問" [--no-rag] [-k 3]

参照の証跡（logs/rag.jsonl に1問1行で残る）:
  retrieved   検索で選ばれたチャンクIDと類似度
  prompt      LLMに実際に渡した全文
  cited       回答中の [チャンクID] 引用
  cite_status 引用判定（OK／NG:引用なし／NG:検索範囲外の引用／記載なし回答）
              [チャンクID] のほか、資料内の規約ID（HR-REV-01 等）での引用も認める
  meta        prompt_eval_count（実際に読んだ入力トークン数）、切り捨て疑い
"""
import json
import math
import os
import re
import sys
from datetime import datetime

import ollama_client as oc

INDEX = "logs/rag_index.json"
LOG = "logs/rag.jsonl"

SYSTEM = (
    "あなたは社内資料に基づいて答えるアシスタントです。"
    "下の【資料】だけを根拠に日本語で答えてください。"
    "根拠にした資料は、各資料の先頭にある角括弧のIDをそのまま書き写して文末に付けてください"
    "（例：〜です。[01_coding_rules#1]）。"
    "【資料】に答えが無い場合は「資料に記載なし」とだけ答えてください。"
)


def chunk_markdown(path):
    """見出し（## ）単位で分割。IDは「ファイル名#連番」。"""
    name = os.path.splitext(os.path.basename(path))[0]
    text = open(path, encoding="utf-8").read()
    title = text.splitlines()[0].lstrip("# ").strip() if text else name
    parts = re.split(r"\n(?=## )", text)
    chunks = []
    for i, p in enumerate(p for p in parts if p.strip().startswith("## ")):
        chunks.append({"id": f"{name}#{i + 1}", "text": f"{title}\n{p.strip()}"})
    return chunks


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cmd_index(folder):
    chunks = []
    for f in sorted(os.listdir(folder)):
        if f.endswith(".md"):
            chunks += chunk_markdown(os.path.join(folder, f))
    vecs = oc.embed([c["text"] for c in chunks])
    for c, v in zip(chunks, vecs):
        c["vec"] = v
    os.makedirs("logs", exist_ok=True)
    json.dump({"emb_model": oc.EMB_MODEL, "chunks": chunks},
              open(INDEX, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"索引作成: {len(chunks)} チャンク（埋め込み: {oc.EMB_MODEL}）")
    for c in chunks:
        print("  ", c["id"], c["text"].splitlines()[1])


def cmd_ask(question, use_rag=True, k=3):
    retrieved = []
    if use_rag:
        idx = json.load(open(INDEX, encoding="utf-8"))
        qv = oc.embed([question])[0]
        scored = sorted(
            ({"id": c["id"], "score": round(cosine(qv, c["vec"]), 4), "text": c["text"]}
             for c in idx["chunks"]),
            key=lambda x: -x["score"])
        retrieved = scored[:k]
        context = "\n\n".join(f"[{r['id']}]\n{r['text']}" for r in retrieved)
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"【資料】\n{context}\n\n【質問】\n{question}"}]
    else:
        messages = [{"role": "user", "content": question}]

    answer, meta = oc.chat(messages)
    cited = set(re.findall(r"\[([\w\-]+#\d+)\]", answer))
    # 資料内の規約ID（例：HR-REV-01）で引用した場合も、そのチャンクへの引用とみなす
    rule_ids = set(re.findall(r"\b([A-Z]{2,}-[A-Z]{2,}-\d+)\b", answer))
    for r in retrieved:
        if any(f"## {rid}" in r["text"] for rid in rule_ids):
            cited.add(r["id"])
    cited = sorted(cited)
    ids = {r["id"] for r in retrieved}
    if "記載" in answer and "なし" in answer:
        cite_status = "記載なし回答"
    elif not cited:
        cite_status = "NG:引用なし"
    elif all(c in ids for c in cited):
        cite_status = "OK"
    else:
        cite_status = "NG:検索範囲外の引用"
    rec = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "mode": "rag" if use_rag else "no-rag",
        "question": question,
        "retrieved": [{"id": r["id"], "score": r["score"]} for r in retrieved],
        "prompt": messages,
        "answer": answer,
        "cited": cited,
        "cited_ok": cite_status in ("OK", "記載なし回答"),
        "cite_status": cite_status,
        "meta": meta,
    }
    os.makedirs("logs", exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"== {rec['mode']} / {meta['model']} / {meta['total_sec']}s")
    for r in rec["retrieved"]:
        print(f"  検索: {r['id']}  類似度 {r['score']}")
    print(f"  入力トークン: {meta['prompt_eval_count']} / num_ctx {meta['num_ctx']}"
          + ("  ※切り捨て疑い" if meta["truncation_suspected"] else ""))
    print("-- 回答")
    print(answer)
    print(f"-- 引用: {cited or 'なし'}  判定: {cite_status}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in ("index", "ask"):
        print(__doc__)
        sys.exit(1)
    if args[0] == "index":
        cmd_index(args[1] if len(args) > 1 else "corpus")
    else:
        k = int(args[args.index("-k") + 1]) if "-k" in args else 3
        q = [a for i, a in enumerate(args[1:], 1)
             if not a.startswith("-") and args[i - 1] != "-k"][0]
        cmd_ask(q, use_rag="--no-rag" not in args, k=k)
