#!/usr/bin/env bash
# ============================================================================
#  一键查看抢机进展
#  用法: ./status.sh
#  许可证：MIT
# ============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B="${A1GRAB_HOME:-$SCRIPT_DIR}"

echo "=============== 服务状态 ==============="
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^a1grab.service'; then
  echo "运行中  : $(systemctl is-active a1grab.service 2>/dev/null)"
  echo "开机自启: $(systemctl is-enabled a1grab.service 2>/dev/null)"
  echo "重启次数: $(systemctl show a1grab -p NRestarts --value 2>/dev/null)"
  echo "启动时刻: $(systemctl show a1grab -p ActiveEnterTimestamp --value 2>/dev/null)"
else
  echo "未安装为 systemd 服务（可能用 nohup 在跑）"
  pgrep -f "grab.sh" >/dev/null 2>&1 && echo "进程    : 正在运行" || echo "进程    : 未运行"
fi

echo
echo "=============== 是否抢到 ==============="
if [ -f "$B/SUCCESS" ]; then
  echo "已抢到！"
  echo "---------------------------------------"
  cat "$B/RESULT.txt" 2>/dev/null || cat "$B/SUCCESS"
else
  echo "尚未抢到。"
fi

echo
echo "=============== 通知渠道 ==============="
[ -f "$B/pushplus_token" ] && echo "PushPlus: 已配置" || echo "PushPlus: 未配置"
[ -f "$B/notify_url" ]     && echo "Webhook : 已配置" || echo "Webhook : 未配置"

echo
echo "=============== 最近 15 条日志 ==============="
tail -n 15 "$B/grab.log" 2>/dev/null || echo "(暂无日志)"
