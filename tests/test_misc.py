"""TG 命令、API 鉴权、监听地址、配置合并、加密密钥文件。"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from engine.api.server import check_listen_host, create_app
from engine.app import Engine
from engine.core.bus import Event
from engine.core.config import Config, load_config
from engine.core.secrets import ChainStore, EncryptedFileStore, env_name
from engine.notify.telegram import StatusProvider, format_event, handle_command, should_send
from engine.storage.db import Database

from .test_engine_e2e import setup_market

D = Decimal


async def _txt() -> str:
    return "ok"


PROVIDER = StatusProvider(_txt, _txt, _txt, _txt, _txt)


async def test_tg_only_owner():
    assert await handle_command("/status", 111, "111", PROVIDER) == "ok"
    assert await handle_command("/status", 222, "111", PROVIDER) is None
    assert await handle_command("/status", 111, None, PROVIDER) is None
    assert "可用命令" in await handle_command("/foo", 111, "111", PROVIDER)


def test_tg_modes_and_format():
    imp = Event("position_opened", {"symbol": "ETHUSDT", "side": "LONG", "qty": "1", "entry": "100",
                                    "stop": "98", "take_profit": "105", "latency_ms": 12}, 0, True)
    scan = Event("scan", {"total": 300, "after_liquidity": 40, "with_signals": 9, "scored": 3,
                          "selected": [{"symbol": "ETHUSDT", "side": "LONG", "score": 72, "reason": "放量"}]}, 0)
    assert should_send(imp, "important") and not should_send(scan, "important")
    assert should_send(scan, "brief") and should_send(scan, "full")
    assert "开仓 ETHUSDT" in format_event(imp)
    assert "300 → 范围 40" in format_event(scan)


def test_listen_host_guard():
    check_listen_host("127.0.0.1")
    check_listen_host("100.101.102.103")  # Tailscale
    with pytest.raises(SystemExit):
        check_listen_host("0.0.0.0")
    with pytest.raises(SystemExit):
        check_listen_host("192.168.1.10")


@pytest.fixture
def api(clock, tmp_path):
    from engine.exchange.mock import MockExchange

    ex = MockExchange(clock=clock)
    setup_market(ex)
    eng = Engine(Config(), ex, Database(tmp_path / "a.db"), clock=clock)
    return eng, TestClient(create_app(eng, "secret-token"))


def test_api_requires_token(api):
    _, c = api
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.get("/api/status", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.json()["safety"] == "PAUSED_NEW"


def test_api_flatten_needs_confirmation(api):
    eng, c = api
    h = {"Authorization": "Bearer secret-token"}
    r = c.post("/api/control/stop", headers=h).json()
    assert r["confirm_required"]
    assert eng.safety.level.value != "HALTED"
    r2 = c.post(f"/api/control/stop?confirm={r['confirm']}", headers=h).json()
    assert r2["ok"] and eng.safety.level.value == "HALTED"


def test_api_ws_rejects_bad_token(api):
    _, c = api
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/events?token=bad") as ws:
        ws.receive_json()


def test_config_local_override(tmp_path):
    d = tmp_path / "default.toml"
    loc = tmp_path / "local.toml"
    d.write_text('[risk]\nrisk_per_trade_pct = 1.0\nmax_leverage = 3\n[scanner.selection]\nmin_score = 60\n')
    loc.write_text('[risk]\nrisk_per_trade_pct = 0.5\n[scanner.selection]\nmin_score = 70\n')
    cfg = load_config(d, loc)
    assert cfg.risk.risk_per_trade_pct == 0.5 and cfg.risk.max_leverage == 3
    assert cfg.scanner.selection.min_score == 70


def test_repo_default_config_loads():
    cfg = load_config()
    assert cfg.engine.listen_host == "127.0.0.1"
    assert cfg.exchange.environment == "demo"


def test_encrypted_store_roundtrip(tmp_path):
    p = tmp_path / "secrets.enc"
    s = EncryptedFileStore(p, "pw")
    s.set("binance.demo.api_key", "abc")
    assert b"abc" not in p.read_bytes()
    assert EncryptedFileStore(p, "pw").get("binance.demo.api_key") == "abc"
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        EncryptedFileStore(p, "wrong")


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv(env_name("telegram.bot_token"), "from-env")
    store = ChainStore(EncryptedFileStore(tmp_path / "s.enc", "pw"))
    assert store.get("telegram.bot_token") == "from-env"
    tok = store.ensure_access_token()
    assert tok and store.ensure_access_token() == tok


def test_no_secrets_in_repo():
    """仓库里不能有任何 Key：检查 config/ 下被跟踪的文件不含可疑字段。"""
    import pathlib
    import re

    for f in pathlib.Path("config").glob("*.toml"):
        if f.name in ("local.toml", "secrets.local.toml"):
            continue
        text = f.read_text()
        assert not re.search(r"(api_key|api_secret|bot_token)\s*=", text), f
