#!/usr/bin/env bash
# ============================================================================
#  抢机通知发送器（可独立运行、可单独测试）
#
#  用法:  ./notify.sh "<标题>" "<正文>"      正文可用 <br> 换行（html 模板）
#  测试:  ./notify.sh "测试" "这是一条测试消息"
#
#  渠道优先级：
#    1. pushplus_token 文件存在  -> 走 PushPlus（推送微信）
#    2. notify_url     文件存在  -> curl 该静态地址（企业微信机器人/Bark/Server酱/钉钉…）
#    3. 都没有                   -> 静默跳过，退出码 0（不能因为通知失败影响抢机）
#
#  许可证：MIT
# ============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B="${A1GRAB_HOME:-$SCRIPT_DIR}"

TITLE="${1:-Oracle A1 抢机通知}"
CONTENT="${2:-（无内容）}"
RESULT="$B/notify_result.json"

# ---------- 渠道 1：PushPlus ----------
# 注意：PushPlus 发消息只需要 token；
#       它的 secretKey 是「开放接口」（查群组/好友/消息记录）用的，发消息用不上。
if [ -f "$B/pushplus_token" ]; then
  TK="$(tr -d ' \t\r\n' <"$B/pushplus_token")"
  if [ -n "$TK" ]; then
    BODY=$(printf '{"token":"%s","title":"%s","content":"%s","template":"html"}' \
            "$TK" "$TITLE" "$CONTENT")
    OUT=$(curl -s -m 25 -X POST "https://www.pushplus.plus/send" \
            -H "Content-Type: application/json" -d "$BODY" 2>&1)
    printf '%s\n' "$OUT" >"$RESULT"
    case "$OUT" in
      *'"code":200'*) echo "PushPlus 推送成功"; exit 0 ;;
      *)              echo "PushPlus 推送失败: $OUT"; exit 1 ;;
    esac
  fi
fi

# ---------- 渠道 2：通用 GET webhook ----------
if [ -f "$B/notify_url" ]; then
  U="$(tr -d ' \t\r\n' <"$B/notify_url")"
  if [ -n "$U" ]; then
    if curl -s -m 20 "$U" >/dev/null 2>&1; then
      echo "webhook 推送成功"; exit 0
    else
      echo "webhook 推送失败"; exit 1
    fi
  fi
fi

echo "未配置任何通知渠道，已跳过。"
exit 0
