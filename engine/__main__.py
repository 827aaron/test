"""命令行入口（App 和 launchd 会调用它；普通用户不需要手动敲）。

  aiquant run                 启动引擎
  aiquant set-secret KEY      把密钥写进钥匙串（交互输入，不回显）
  aiquant check               检查配置、密钥、交易所连通性
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys
from pathlib import Path

from engine.core.config import load_config
from engine.core.secrets import KEYS, default_store

log = logging.getLogger("aiquant")


def _exchange(cfg, store):
    from engine.exchange.binance import BinanceFuturesClient

    env = cfg.exchange.environment
    if cfg.engine.mode == "testnet" and env == "live":
        raise SystemExit("配置冲突：engine.mode=testnet 但 exchange.environment=live")
    if env == "live" and cfg.engine.mode != "live":
        raise SystemExit("实盘需要 engine.mode=live（并在 App 里二次确认）")
    key = store.get(f"binance.{env}.api_key")
    secret = store.get(f"binance.{env}.api_secret")
    if not key or not secret:
        raise SystemExit(f"缺少币安 Key：请运行 `aiquant set-secret binance.{env}.api_key` 和 "
                         f"`aiquant set-secret binance.{env}.api_secret`（或在 App 的连接页填写）")
    return BinanceFuturesClient(env, key, secret, cfg.exchange.recv_window_ms)


async def _run() -> None:
    import uvicorn

    from engine.api.server import check_listen_host, create_app
    from engine.app import Engine
    from engine.notify.telegram import StatusProvider, TelegramNotifier
    from engine.storage.db import Database

    cfg = load_config()
    check_listen_host(cfg.engine.listen_host)
    data_dir = Path(cfg.engine.data_dir)
    store = default_store(data_dir)
    token = store.ensure_access_token()
    engine = Engine(cfg, _exchange(cfg, store), Database(data_dir / "aiquant.db"))

    tasks = []
    if cfg.telegram.enabled:
        bot, owner = store.get("telegram.bot_token"), store.get("telegram.owner_chat_id")
        if bot and owner:
            async def pause() -> str:
                engine.pause_by_user()
                return "已暂停开新单（持仓保护继续）"

            async def stop() -> str:
                engine.halt_by_user()
                return "已停机：不再开新单，已有持仓仍受止损保护"

            tg = TelegramNotifier(bot, owner, cfg.telegram.mode, StatusProvider(
                engine.text_status, engine.text_positions, engine.text_pnl, pause, stop))
            engine.bus.subscribe(tg.on_event)
            tasks.append(asyncio.create_task(tg.run()))
        else:
            log.warning("Telegram 已启用，但缺少 telegram.bot_token / telegram.owner_chat_id")

    if not await engine.start():
        log.error("引擎启动检查未通过，已停机。详见上面的告警。")
    engine.run_background()

    server = uvicorn.Server(uvicorn.Config(create_app(engine, token), host=cfg.engine.listen_host,
                                           port=cfg.engine.listen_port, log_level="info"))
    stop_ev = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_ev.set)
    srv = asyncio.create_task(server.serve())
    await stop_ev.wait()
    server.should_exit = True
    await srv
    await engine.stop()
    for t in tasks:
        t.cancel()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="aiquant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("set-secret")
    s.add_argument("key", choices=sorted(KEYS))
    sub.add_parser("check")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        asyncio.run(_run())
    elif args.cmd == "set-secret":
        cfg = load_config()
        store = default_store(Path(cfg.engine.data_dir))
        value = getpass.getpass(f"{KEYS[args.key]}：")
        if not value:
            sys.exit("没有输入内容")
        store.set(args.key, value.strip())
        print("已保存（存在本机钥匙串/加密文件里，不会进仓库）")
    elif args.cmd == "check":
        asyncio.run(_check())


async def _check() -> None:
    cfg = load_config()
    store = default_store(Path(cfg.engine.data_dir))
    print(f"环境：{cfg.exchange.environment}，模式：{cfg.engine.mode}")
    ex = _exchange(cfg, store)
    try:
        await ex.connect()
        insts = await ex.load_instruments()
        bal = await ex.balance()
        perms = await ex.api_key_permissions()
        print(f"✅ 连接成功：{len(insts)} 个 U 本位永续合约；权益 {bal.equity} USDT")
        print(f"   Key 权限：合约 {perms.can_trade_futures}，提现 {perms.can_withdraw}，"
              f"IP 白名单 {perms.ip_restricted}{'' if perms.checked else '（此环境无法自动检查）'}")
    finally:
        await ex.close()


if __name__ == "__main__":
    main()
