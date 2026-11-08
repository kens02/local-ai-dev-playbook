"""Ollama ローカルAPIの最小クライアント（標準ライブラリのみ・外部通信なし）。

接続先は localhost の Ollama のみ。モデル名・コンテキスト長は環境変数で切り替える。
  LLM_MODEL    生成モデル     （既定: granite3.3:8b）
  EMB_MODEL    埋め込みモデル （既定: granite-embedding:278m）
  NUM_CTX      コンテキスト長 （既定: 16384）
  OLLAMA_HOST  接続先         （既定: http://127.0.0.1:11434）
"""
import json
import os
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "granite3.3:8b")
EMB_MODEL = os.environ.get("EMB_MODEL", "granite-embedding:278m")
NUM_CTX = int(os.environ.get("NUM_CTX", "16384"))


def _post(path, payload):
    req = urllib.request.Request(
        HOST + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as res:
        return json.loads(res.read().decode("utf-8"))


def embed(texts):
    """テキストのリストを埋め込みベクトルのリストにする。"""
    return _post("/api/embed", {"model": EMB_MODEL, "input": texts})["embeddings"]


def chat(messages, schema=None):
    """1回の生成。戻り値は (本文, 計測値dict)。

    計測値の prompt_eval_count は「モデルが実際に読んだ入力トークン数」。
    資料を渡したのに値が小さい／NUM_CTX に張り付く場合は、切り捨てを疑う。
    """
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0},
    }
    if schema is not None:
        payload["format"] = schema  # 構造化出力（JSONスキーマ）
    res = _post("/api/chat", payload)
    meta = {
        "model": LLM_MODEL,
        "num_ctx": NUM_CTX,
        "prompt_eval_count": res.get("prompt_eval_count"),
        "eval_count": res.get("eval_count"),
        "total_sec": round(res.get("total_duration", 0) / 1e9, 1),
    }
    pec = meta["prompt_eval_count"] or 0
    # Ollamaは入力が溢れると先頭側を捨て、上限の約半分まで詰めることがある（RAG-05で実測）。
    # そのため「上限の50%以上を使っている」時点で切り捨てを疑う。確定はサーバーログで確認する。
    meta["truncation_suspected"] = pec >= NUM_CTX * 0.5
    return res["message"]["content"], meta
