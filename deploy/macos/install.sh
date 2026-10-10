#!/bin/bash
# 在 Mac mini 上安装引擎后台服务（阶段 4 的 App 会代替这一步）。
# 用法：bash deploy/macos/install.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

if ! command -v uv >/dev/null 2>&1; then
  echo "安装 uv（Python 环境管理工具）..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
UV="$(command -v uv)"

echo "安装依赖..."
"$UV" sync --frozen
mkdir -p data/logs

echo
echo "检查密钥（存在 macOS 钥匙串里，不会进仓库）..."
for k in binance.demo.api_key binance.demo.api_secret; do
  if ! "$UV" run python -c "import keyring,sys; sys.exit(0 if keyring.get_password('aiquant','$k') else 1)"; then
    echo "需要填写 $k："
    "$UV" run aiquant set-secret "$k"
  fi
done
read -r -p "要配置 Telegram 吗？(y/N) " yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
  "$UV" run aiquant set-secret telegram.bot_token
  "$UV" run aiquant set-secret telegram.owner_chat_id
  if [ ! -f config/local.toml ] || ! grep -q '^\[telegram\]' config/local.toml; then
    printf '\n[telegram]\nenabled = true\nmode = "important"\n' >> config/local.toml
  fi
fi

echo
echo "测试连接..."
"$UV" run aiquant check

PLIST="$HOME/Library/LaunchAgents/com.aiquant.engine.plist"
sed -e "s#__REPO__#$REPO#g" -e "s#__UV__#$UV#g" deploy/macos/com.aiquant.engine.plist > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "✅ 已安装并启动。日志：$REPO/data/logs/engine.log"
echo "   停止：launchctl bootout gui/$(id -u) $PLIST"
