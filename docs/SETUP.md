# 阶段 1 · 在 Mac mini 上实测

> 阶段 1 还没有 App（App 和首次启动向导在阶段 4 做），所以这一阶段需要在「终端」里运行**一条安装命令**，之后都是自动的。
> 所有 Key 只存在你 Mac 的钥匙串里，**不会进仓库**。

## 1. 准备 Key

### 币安 Demo Trading（模拟盘）Key
币安的 U 本位合约“测试网”现在叫 **Demo Trading**（地址 `demo-fapi.binance.com`），NautilusTrader 也是连这里。

1. 登录币安 → 切换到 **Demo Trading（模拟交易）** → 合约。
2. 在 Demo Trading 的 **API 管理** 里新建一个 API Key。
3. 权限：**只勾“允许合约交易”**，**不要勾“允许提现”**（开了提现的 Key 引擎会拒绝使用）。
4. 复制 API Key 和 Secret，下一步要用。

> 如果你用的是旧版测试网 `testnet.binancefuture.com` 的 Key，把 `config/local.toml` 里的 `[exchange] environment` 改成 `"testnet"`。

### Telegram（可选）
1. 在 TG 里找 **@BotFather** → `/newbot` → 拿到 **Bot Token**。
2. 在 TG 里找 **@userinfobot** → 它会回复你的数字 **ID**（机器人只响应这个 ID）。
3. 先给你的新机器人发一句话（否则它没法给你发消息）。

## 2. 安装（只需一次）

打开「终端」，依次粘贴：

```bash
git clone https://github.com/827aaron/test.git aiquant
cd aiquant
git checkout claude/eloquent-thompson-bqbxam
bash deploy/macos/install.sh
```

安装脚本会：装好 Python 环境 → 提示你粘贴币安 Key / Secret（输入时不显示）→ 可选配置 TG → 测试连接 → 注册成开机自启的后台服务。

## 3. 改设置（可选）

不要改 `config/default.toml`。需要改什么，就在 `config/local.toml` 里写要覆盖的项，例如：

```toml
[risk]
risk_per_trade_pct = 0.5   # 只能调严；超过硬上限的值会被忽略

[scanner]
scope = "top30"
```

改完重启服务：`launchctl kickstart -k gui/$(id -u)/com.aiquant.engine`

## 4. 阶段 1 验收清单（请在 Demo Trading 上逐项实测）

| # | 测试 | 怎么做 | 期望 |
|---|---|---|---|
| 1 | 连接 | `uv run aiquant check` | 显示合约数量和余额 |
| 2 | 扫描 | 等 5 分钟 | TG 收到扫描漏斗（简要模式）；`data/aiquant.db` 的 `scanrun` 表有记录 |
| 3 | 下单 | 等扫描器选中一个币 | 限价入场成交 → 网页端能看到 **止损** 和 **止盈** 条件单（在“条件单 / 策略委托”里） |
| 4 | 补挂止损 | 在网页端手动撤掉止损单 | ≤ 60 秒内（下一次对账）自动补挂，TG 提示 |
| 5 | 断线恢复 | 关掉 Wi-Fi 1 分钟再打开 | TG 提示暂停 → 恢复后对账 → 自动恢复开新单；持仓的止损单一直在交易所上 |
| 6 | 重启恢复 | `launchctl kickstart -k gui/$(id -u)/com.aiquant.engine` | 重启后认领已有持仓，止损仍在 |
| 7 | 当日停机 | 把 `daily_loss_halt_pct` 调到 `0.1` 并重启 | 亏到 0.1% 后停机，不再开新单 |
| 8 | TG 命令 | 发 `/status` `/pos` `/pnl` `/pause` `/stop` | 正常回复；`/stop` 需要点确认；别人的账号发命令没有任何反应 |
| 9 | 完整交易 | 跑 1–2 天 | 至少 5 笔完整交易，每笔都有止损，`position` 表有盈亏记录 |

有任何一项不符合，把 `data/logs/engine.log` 发给我。

## 5. 常用操作

- 看日志：`tail -f data/logs/engine.log`
- 停止服务：`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aiquant.engine.plist`
- 重新填 Key：`uv run aiquant set-secret binance.demo.api_key`
- 本地 API（App 以后用）：`http://127.0.0.1:8765/api/status`，访问令牌存在钥匙串的 `engine.access_token`
