#!/bin/zsh
# start-ollama.sh  使い方: zsh start-ollama.sh [コンテキスト長]
# 事前にメニューバーの Ollama アプリを終了しておくこと（ポートが競合し、ここでの設定も効かない）
export OLLAMA_CONTEXT_LENGTH=${1:-16384}   # スクリプト側の既定 NUM_CTX と揃える
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0           # Flash Attention が前提
export OLLAMA_MAX_LOADED_MODELS=2          # 埋め込み＋生成モデルを同時に保持
export OLLAMA_KEEP_ALIVE=30m
export OLLAMA_HOST=127.0.0.1:11434         # 外部から接続させない
export OLLAMA_NO_CLOUD=1                   # クラウド機能を無効化（モデルの pull は先に済ませておく）
# サーバーログはホームに残す（ユーザー名などを含むため、このリポジトリには置かない）
ollama serve 2>&1 | tee -a ~/ollama-server.log
