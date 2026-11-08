"""LLM Wiki 参照確認スクリプト（Karpathy の LLM Wiki パターンの最小実装）。

ベクトル検索は使わない。LLMが資料を読んで Markdown の Wiki を書き、
質問時は index.md（目次）を見て開くページを自分で選び、そのページだけを根拠に答える。

使い方:
  python3 wiki_check.py init
  python3 wiki_check.py ingest 資料.md          # 資料を取り込み、Wikiを更新
  python3 wiki_check.py ask "質問" [--save]      # --save で回答をWikiに書き戻す
  python3 wiki_check.py reindex                  # 目次だけを作り直す（LLMは使わない）

参照の証跡:
  wiki/log.md           取り込み・質問の時系列記録（人が読む用）
  logs/wiki.jsonl       選んだページ・回答・引用・トークン数（機械的確認用）
  git diff / git log    取り込みごとにWikiの何が変わったか（git があれば自動コミット）
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import ollama_client as oc

W = "wiki"
LOG = "logs/wiki.jsonl"

SCHEMA_DOC = """# Wiki 運用ルール（スキーマ）
- pages/ 配下に「1規約・1概念につき1ページ」で書く。資料1本を1ページに丸写ししない。
  例：資料に規約が3つあれば、ページも3つ作る。
- ファイル名は英小文字・数字・ハイフンのみ。内容を表す英語名にする（例：pages/error-code.md、pages/log-output.md）。
  資料のファイル名（01_xxx など）をページ名に使わない。
- 各ページの末尾に「出典: [[sources/<元ファイル名>]]」を必ず書く。
- 新しい資料が古い記述と矛盾する場合、ページを新しい内容に更新し、
  「## 変更履歴」に旧→新と出典を残す。
- index.md（目次）と出典リンクの付与はスクリプトが機械的に行う（台帳は機械、内容はLLM）。
"""

INGEST_FORMAT = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["pages", "conflicts", "summary"],
}

SELECT_FORMAT = {
    "type": "object",
    "properties": {"pages": {"type": "array", "items": {"type": "string"}}},
    "required": ["pages"],
}


def rd(p):
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def wr(p, s):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(s)


def append_log(line):
    with open(f"{W}/log.md", "a", encoding="utf-8") as f:
        f.write(f"- {datetime.now():%Y-%m-%d %H:%M} {line}\n")


def jlog(rec):
    os.makedirs("logs", exist_ok=True)
    rec["time"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def safe_page(path):
    """LLMが返したパスを pages/*.md に限定する（Wiki外への書き込み防止）。

    戻り値 (正規化後パス, 状態)。状態は "ok"／"normalized"（名前の規則違反を補正）／"rejected"。
    pages/ 以外や「..」を含むパスは拒否。名前の文字種違反は拒否せず補正し、件数として記録する。
    """
    raw = path.strip().lstrip("/").replace("[[", "").replace("]]", "")
    prefixed = False
    if "/" not in raw and ".." not in raw:
        raw, prefixed = "pages/" + raw, True  # 「pages/」の付け忘れは補正（WIKI検証で実測）
    if ".." in raw or not raw.startswith("pages/") or raw.count("/") != 1:
        return raw, "rejected"
    stem = raw[len("pages/"):]
    if stem.endswith(".md"):
        stem = stem[:-3]
    fixed = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9\-]", "-", stem.lower())).strip("-")
    if not fixed:
        return raw, "rejected"
    return f"pages/{fixed}.md", ("ok" if fixed == stem and not prefixed else "normalized")


def relink(text, renames):
    """補正したページ名に合わせて [[pages/旧名]] のリンクを書き換える。"""
    for old, new in renames.items():
        text = text.replace(f"[[{old[:-3]}]]", f"[[{new[:-3]}]]").replace(f"[[{old}]]", f"[[{new[:-3]}]]")
    return text


def page_title(text):
    for line in text.splitlines():
        t = line.strip().lstrip("#").strip()
        if t:
            return t[:60]
    return "(無題)"


def page_headings(text):
    """ページ内の小見出し（変更履歴・出典を除く）を返す。目次の検索性を上げるために使う。"""
    hs = [h.strip() for h in re.findall(r"^#{2,3}\s+(.+)$", text, flags=re.M)]
    return [h for h in hs if not h.startswith(("変更履歴", "出典"))]


def rebuild_index():
    """index.md を実在ページから機械的に再生成する（目次の不整合を構造的に無くす）。

    各行は「ページ名 タイトル — 小見出しの列挙」。タイトルだけだと、粗いページの中身
    （例：ログ出力の規約）が目次から見えず、質問時のページ選択を誤る（WIKI検証で実測）。
    """
    pages = all_pages()
    lines = ["# Index", ""]
    for p, c in pages.items():
        hs = page_headings(c)
        detail = (" — " + "／".join(hs))[:200] if hs else ""
        lines.append(f"- [[{p[:-3]}]] {page_title(c)}{detail}")
    wr(f"{W}/index.md", "\n".join(lines) + "\n")
    return len(pages)


def git_commit(msg):
    if shutil.which("git") is None:
        return
    if not os.path.isdir(f"{W}/.git"):
        subprocess.run(["git", "init", "-q"], cwd=W)
    subprocess.run(["git", "add", "-A"], cwd=W)
    subprocess.run(["git", "-c", "user.name=wiki-check", "-c", "user.email=wiki@local",
                    "commit", "-q", "-m", msg], cwd=W)


def all_pages():
    d = f"{W}/pages"
    if not os.path.isdir(d):
        return {}
    return {f"pages/{f}": rd(f"{d}/{f}") for f in sorted(os.listdir(d)) if f.endswith(".md")}


def cmd_init():
    os.makedirs(f"{W}/pages", exist_ok=True)
    os.makedirs(f"{W}/sources", exist_ok=True)
    wr(f"{W}/SCHEMA.md", SCHEMA_DOC)
    if not os.path.exists(f"{W}/index.md"):
        wr(f"{W}/index.md", "# Index\n")
    if not os.path.exists(f"{W}/log.md"):
        wr(f"{W}/log.md", "# Log\n")
    git_commit("init")
    print("wiki/ を初期化しました")


def cmd_ingest(src):
    name = os.path.basename(src)
    shutil.copy(src, f"{W}/sources/{name}")
    existing = all_pages()
    existing_txt = "\n\n".join(f"=== {p} ===\n{c}" for p, c in existing.items()) or "（まだ無し）"
    messages = [
        {"role": "system", "content": "あなたはWikiの管理者です。次のルールに厳密に従います。\n" + SCHEMA_DOC},
        {"role": "user", "content":
            f"【現在の index.md】\n{rd(W + '/index.md')}\n\n"
            f"【既存ページ】\n{existing_txt}\n\n"
            f"【新しい資料: sources/{name}】\n{rd(src)}\n\n"
            "新しい資料をWikiに統合してください。新規・更新したページだけを pages に入れ、"
            "content はページ全文にしてください。"
            "既存の記述と矛盾した点は conflicts に列挙し、summary に変更概要を1〜2文で書いてください。"},
    ]
    out, meta = oc.chat(messages, schema=INGEST_FORMAT)
    data = json.loads(out)

    written, rejected, no_source, renames = [], [], [], {}
    accepted = []
    for pg in data["pages"]:
        path, status = safe_page(pg["path"])
        if status == "rejected":
            rejected.append(pg["path"])
            continue
        if status == "normalized":
            renames[pg["path"].strip().lstrip("/")] = path
        accepted.append((path, pg["content"]))
    src_pat = r"\[\[sources/[^\]]+\]\]"
    unchanged, restored, coarse, guarded = [], {}, [], {}
    for path, content in accepted:
        content = relink(content, renames)
        old = existing.get(path)
        if old is not None:
            # 既存ページの書き直し：本文（出典行を除く）が同じなら書かない（無駄な書き換えの検出）
            # 出典の書式揺れ（「### 出典:」見出し等）は比較から除く
            strip = lambda t: re.sub(r"^#*\s*出典:.*$|\[\[sources/[^\]]+\]\]", "", t, flags=re.M).strip()
            if strip(old) == strip(content):
                unchanged.append(path)
                continue
            # 既存の規約見出し（## の直後のID）が消える更新は自動反映しない。
            # WIKI-03 で、改訂の取り込み時に無関係な規約（ログ・命名）が丸ごと消え、
            # 規約IDまで書き換えられた（HR-STD-01→HRX-STD-01）ことへの対策。
            sec_ids = lambda t: set(re.findall(r"^## (?!変更履歴)(\S+)", t, flags=re.M))
            lost_ids = sorted(sec_ids(old) - sec_ids(content))
            if lost_ids:
                prop = f"{W}/proposals/{os.path.basename(path)}"
                wr(prop, content)
                guarded[path] = {"lost_sections": lost_ids, "proposal": prop}
                continue
            # LLMが落とした既存の出典を復元する（出典の消失・すり替えを防ぐ）
            lost = sorted(set(re.findall(src_pat, old)) - set(re.findall(src_pat, content)))
            if lost:
                restored[path] = lost
                content = content.rstrip() + "\n\n出典: " + " ".join(lost) + "\n"
        elif f"[[sources/{name}]]" not in content:
            # 新規ページに出典が無い場合のみ、今回の資料を出典として付与する
            no_source.append(path)
            content = content.rstrip() + f"\n\n出典: [[sources/{name}]]\n"
        if len(re.findall(r"^## (?!変更履歴)", content, flags=re.M)) > 1:
            coarse.append(path)  # 1ページに複数の見出し＝1概念1ページの規則違反の疑い
        written.append(path)
        wr(f"{W}/{path}", content)
    n_pages = rebuild_index()
    append_log(f"ingest sources/{name}: {data['summary']} 更新={written} 矛盾={data['conflicts']}")
    git_commit(f"ingest {name}")
    jlog({"op": "ingest", "source": name, "written": written, "rejected": rejected,
          "renamed": renames, "pages_total": n_pages, "unchanged_rewrites": unchanged,
          "restored_sources": restored, "coarse_pages": coarse, "guarded_updates": guarded,
          "no_source_link": no_source, "conflicts": data["conflicts"],
          "summary": data["summary"], "meta": meta})

    print(f"== ingest {name} / {meta['total_sec']}s / 入力トークン {meta['prompt_eval_count']}"
          + ("  ※切り捨て疑い" if meta["truncation_suspected"] else ""))
    print("  更新ページ:", written)
    if rejected:
        print("  ※規則外パスで書き込み拒否:", rejected)
    if renames:
        print("  ※ページ名を規則に合わせて補正:", renames)
    if no_source:
        print("  ※出典リンクが無かった新規ページ（自動付与済み）:", no_source)
    for gp, g in guarded.items():
        print(f"  ※既存の規約 {g['lost_sections']} が消える更新のため {gp} は保留（要レビュー）→ {g['proposal']}")
    if restored:
        print("  ※LLMが消した既存の出典を復元:", restored)
    if unchanged:
        print("  ※内容が同じなので書き込まなかったページ:", unchanged)
    if coarse:
        print("  ※1ページに複数の規約（1概念1ページ違反の疑い）:", coarse)
    print(f"  index.md 再生成: 全 {n_pages} ページ")
    print("  矛盾検出:", data["conflicts"] or "なし")
    print("  概要:", data["summary"])


def cmd_ask(question, save=False):
    index = rd(f"{W}/index.md")
    # 1段目: 目次だけを見て、開くページをLLMが選ぶ（=何を参照しに行ったかの証跡）
    sel, meta1 = oc.chat([
        {"role": "system", "content": "Wikiの目次から、質問に答えるために読むべきページを最大3つ選び、パスで返してください。"},
        {"role": "user", "content": f"【index.md】\n{index}\n\n【質問】\n{question}"},
    ], schema=SELECT_FORMAT)
    pages = all_pages()
    opened, missing = [], []
    for p in json.loads(sel)["pages"]:
        path, status = safe_page(p)
        (opened if status != "rejected" and path in pages else missing).append(path)

    # 2段目: 開いたページの本文だけを根拠に回答
    body = "\n\n".join(f"=== [[{p[:-3]}]] ===\n{pages[p]}" for p in opened) or "（該当ページ無し）"
    answer, meta2 = oc.chat([
        {"role": "system", "content":
            "下の【Wikiページ】だけを根拠に日本語で答えてください。"
            "根拠にしたページを [[pages/xxx]] の形で文中に必ず書いてください。"
            "答えが無ければ「Wikiに記載なし」とだけ答えてください。"},
        {"role": "user", "content": f"【Wikiページ】\n{body}\n\n【質問】\n{question}"},
    ])
    cited = sorted(set(re.findall(r"\[\[(pages/[a-z0-9\-]+)\]\]", answer)))
    cited_ok = all(f"{c}.md" in opened for c in cited)

    append_log(f"ask 「{question}」 開いたページ={opened} 引用={cited}")
    if save and cited and cited_ok:  # 根拠の確かな回答だけを書き戻す
        slug = "qa-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        wr(f"{W}/pages/{slug}.md", f"# Q&A: {question}\n\n{answer}\n\n出典: {' '.join(f'[[{c}]]' for c in cited)}\n")
        rebuild_index()
        append_log(f"file-back pages/{slug}.md")
    git_commit(f"ask {question[:30]}")
    jlog({"op": "ask", "question": question, "opened": opened, "missing": missing,
          "answer": answer, "cited": cited, "cited_ok": cited_ok,
          "meta_select": meta1, "meta_answer": meta2})

    print(f"== ask / {meta2['model']} / {meta1['total_sec'] + meta2['total_sec']:.1f}s")
    print("  開いたページ:", opened, ("  ※存在しないページを要求: " + str(missing)) if missing else "")
    print(f"  入力トークン: {meta2['prompt_eval_count']} / num_ctx {meta2['num_ctx']}")
    print("-- 回答")
    print(answer)
    print(f"-- 引用: {cited or 'なし'}  開いたページの範囲内: {cited_ok}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("init", "ingest", "ask", "reindex"):
        print(__doc__)
        sys.exit(1)
    if a[0] == "init":
        cmd_init()
    elif a[0] == "reindex":
        print(f"index.md 再生成: 全 {rebuild_index()} ページ")
        print(rd(f"{W}/index.md"))
    elif a[0] == "ingest":
        cmd_ingest(a[1])
    else:
        cmd_ask([x for x in a[1:] if not x.startswith("--")][0], save="--save" in a)
