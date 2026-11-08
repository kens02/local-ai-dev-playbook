# Lab 01：ローカルLLMが資料を参照したかを確かめる（RAG / LLM Wiki）

- 対象レベル：入門〜実践
- このページで分かること：ローカルLLMが「資料を読んで答えたか」を証跡で確認する手順。RAG と LLM Wiki を同じ資料・同じモデルで比べる方法
- 最終確認日：2026-11-08（Ollama 0.40.14 で確認）

## 1. 概要

Python 標準ライブラリだけで動く、最小の検証キットです（pip 不要）。通信先は localhost の Ollama だけです。

資料には、モデルが事前に知り得ない値（カナリア値）を埋め込んであります。正しく答えられれば、資料を参照した証拠になります。

| 証跡 | RAG | LLM Wiki |
|---|---|---|
| ① 何を探したか | 検索されたチャンクと類似度 | 目次から LLM が選んだページ |
| ② 何を渡したか | プロンプト全文（logs/rag.jsonl） | 開いたページの本文 |
| ③ 実際に読んだ量 | `prompt_eval_count`（入力トークン数） | 同左 |
| ④ 何を根拠と言ったか | 回答中の引用と判定 | 回答中の引用と判定 |

LLM Wiki は、取り込みのたびに `wiki/log.md` と git 履歴で「知識がどう書き換わったか」も追えます。

## 2. ファイル

| ファイル | 役割 |
|---|---|
| start-ollama.sh | Ollama の起動（コンテキスト長、同時ロード数、クラウド無効化） |
| ollama_client.py | Ollama API の呼び出し（埋め込み・生成・トークン計測・切り捨て疑いの判定） |
| rag_check.py | RAG：索引化・質問・証跡ログ・引用判定 |
| wiki_check.py | LLM Wiki：初期化・取り込み・質問・書き戻し・目次の再生成・安全装置 |
| corpus/ | 架空の開発標準3本（コーディング規約・レビュー手順・単体テスト方針） |
| corpus_v2/ | 改訂資料1本（矛盾・更新の検証用） |
| logs/、wiki/ | 実行時に生成される（.gitignore 済み） |

## 3. 準備（Apple Silicon・メモリ16GB で確認）

```zsh
ollama pull granite-embedding:278m   # 埋め込み（IBM、Apache 2.0、日本語対応）
ollama pull granite3.3:8b            # 生成（IBM、Apache 2.0）
```

1. メニューバーの Ollama アプリを終了する
2. ターミナル1：このフォルダで `zsh start-ollama.sh` を実行し、開いたままにする
3. ターミナル2：**このフォルダをカレントディレクトリにして** 以降のコマンドを実行する（スクリプトは相対パスで動く）

環境変数で切り替えられます：`LLM_MODEL`、`EMB_MODEL`、`NUM_CTX`（既定 16384）、`OLLAMA_HOST`

## 4. RAG の手順

```zsh
python3 rag_check.py index
python3 rag_check.py ask "例外メッセージの接頭辞は？"
python3 rag_check.py ask "例外メッセージの接頭辞は？" --no-rag
```

## 5. LLM Wiki の手順

```zsh
python3 wiki_check.py init
python3 wiki_check.py ingest corpus/01_coding_rules.md
python3 wiki_check.py ingest corpus/02_review_flow.md
python3 wiki_check.py ingest corpus/03_test_policy.md
python3 wiki_check.py ask "例外メッセージの接頭辞は？"
python3 wiki_check.py ingest corpus_v2/04_coding_rules_rev1.md
python3 wiki_check.py ask "ログの保存期間は？"
python3 wiki_check.py reindex                 # 目次だけ作り直す（LLM不要）
cd wiki && git log -p && cd ..                # 何が変わったか
```

取り込みは1本あたり1〜2分かかります（資料が増えるほど長くなります）。

## 6. 試験シナリオ

| ID | 内容 | 合格の目安 |
|---|---|---|
| RAG-01 | カナリア質問を RAG あり／なしで比較 | ありは正答＋正しい引用。なしは不正解か「分からない」 |
| RAG-02 | 資料にない質問（例：「コードレビューの所要時間の上限は？」） | 「記載なし」と答える |
| RAG-03 | 引用の妥当性 | 判定が OK（引用0件・検索範囲外の引用は NG） |
| RAG-04 | 言い換え質問（例：「エラー文の頭に付ける記号は？」） | 正しいチャンクが上位3件に入る |
| RAG-05 | `NUM_CTX=256` で実行 | 入力が切り捨てられ、回答が劣化する（サーバーログで確定） |
| RAG-06 | `corpus_all/`（corpus＋corpus_v2）を索引化して同じ質問 | 新旧どちらで答えるかを記録 |
| WIKI-01 | 3資料の取り込み | ページ生成、出典リンク、目次、git 差分 |
| WIKI-02 | 質問時の参照経路 | 選んだページが妥当、引用がその範囲内 |
| WIKI-03 | 改訂資料の取り込み | 矛盾検出、ページが新しい値に更新、無関係な規約が消えない |
| WIKI-04 | `ask ... --save` 後に類似質問 | 書き戻したページが参照される |
| WIKI-05 | 規則違反の件数 | 補正・拒否・出典なし・保留の件数を記録 |

RAG-06 の準備：

```zsh
mkdir -p corpus_all && cp corpus/*.md corpus_v2/*.md corpus_all/
python3 rag_check.py index corpus_all
```

RAG-05 の確定：

```zsh
grep "truncating input prompt" ~/ollama-server.log
```

### カナリア質問と正解

| 質問 | 正解（改訂前 → 改訂後） |
|---|---|
| 例外メッセージの接頭辞は？ | HRE- → HRX- |
| 接頭辞の数字の上1桁が4のときは何層？ | 業務層 |
| ログの保存期間は？ | 37日 → 90日 |
| マージ要求の承認者は何名で、誰が務める？ | 2名、うち1名は若葉番 |
| 分岐網羅率の合格ラインは？ | 83% 以上 |

## 7. 安全装置と補正（wiki_check.py）

小型モデルは書式や規則を守れないことがあるため、次の処理をスクリプトで行います。

| 処理 | 内容 |
|---|---|
| ページ名の補正 | 文字種違反・`pages/` の付け忘れを補正。Wiki 外への書き込みは拒否 |
| 目次の機械生成 | 実在ページと小見出しから毎回作り直す |
| 出典の管理 | 新規ページには取り込んだ資料を付与。既存ページから消えた出典は復元 |
| 更新の保留 | 既存の規約見出し（ID）が消える更新は反映せず、`wiki/proposals/` に退避 |
| 書き戻しの制限 | 引用が正しい回答だけを Wiki に保存 |

## 8. 既知の限界

- LLM Wiki の取り込みは既存ページ全文を毎回渡すため、ページが増えると成立しない
- 引用の判定は「検索・選択の範囲内か」まで。引用先に回答の内容が本当にあるか（取り違え）は判定しない
- 埋め込みモデルの入力上限は 512 トークン
- temperature 0 でも完全には再現しない。各シナリオ3回の実行を推奨

## 9. 検証結果

[docs/40-local-ai-env/rag-vs-llmwiki-verification.md](../../docs/40-local-ai-env/rag-vs-llmwiki-verification.md)

## 10. AI の利用について

このキットと文書は、生成AI（Anthropic の Claude）を使って作成し、作成者が実機で動作と結果を確認しています。
