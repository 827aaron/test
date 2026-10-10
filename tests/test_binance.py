"""币安客户端：用 httpx.MockTransport 模拟服务器，验证签名、路由（普通单 vs Algo 条件单）和解析。

样例数据按币安官方文档的格式编写（云端连不上币安，无法录制真实响应）。
"""

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from engine.core.types import OrderRequest, OrderSide, OrderStatus, OrderType, Side
from engine.exchange.base import OrderRejected
from engine.exchange.binance import (
    ENDPOINTS,
    BinanceFuturesClient,
    parse_instruments,
    parse_position,
    parse_user_message,
    sign,
)

D = Decimal

EXCHANGE_INFO = {"symbols": [
    {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "baseAsset": "BTC",
     "quoteAsset": "USDT", "onboardDate": 1569398400000, "filters": [
         {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
         {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
         {"filterType": "MIN_NOTIONAL", "notional": "100"}]},
    {"symbol": "BTCUSDT_251226", "contractType": "CURRENT_QUARTER", "status": "TRADING", "baseAsset": "BTC",
     "quoteAsset": "USDT", "filters": []},
    {"symbol": "ETHBTC", "contractType": "PERPETUAL", "status": "TRADING", "baseAsset": "ETH",
     "quoteAsset": "BTC", "filters": []},
]}


class FakeBinance:
    def __init__(self):
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((req.method, url.path, q, dict(req.headers)))
        path = url.path
        if path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_760_000_000_000})
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=EXCHANGE_INFO)
        if path == "/fapi/v1/order" and req.method == "POST":
            if q["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                return httpx.Response(400, json={"code": -4120, "msg": "STOP_ORDER_SWITCH_ALGO"})
            return httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "12"}, json={
                "symbol": q["symbol"], "clientOrderId": q["newClientOrderId"], "orderId": 42, "side": q["side"],
                "type": q["type"], "origType": q["type"], "status": "NEW", "origQty": q["quantity"],
                "executedQty": "0", "avgPrice": "0", "price": q.get("price", "0"), "stopPrice": "0",
                "reduceOnly": q.get("reduceOnly") == "true", "closePosition": False, "updateTime": 1})
        if path == "/fapi/v1/algoOrder" and req.method == "POST":
            return httpx.Response(200, json={
                "algoId": 7, "clientAlgoId": q["clientAlgoId"], "algoType": "CONDITIONAL",
                "orderType": q["type"], "symbol": q["symbol"], "side": q["side"], "algoStatus": "NEW",
                "triggerPrice": q["triggerPrice"], "quantity": q.get("quantity", "0"),
                "closePosition": q.get("closePosition") == "true", "reduceOnly": False, "createTime": 1})
        if path == "/fapi/v1/algoOrder" and req.method == "DELETE":
            return httpx.Response(200, json={"algoId": 7, "clientAlgoId": q["clientAlgoId"], "code": "200",
                                             "msg": "success"})
        if path == "/fapi/v1/order" and req.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change margin type."})
        if path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": int(q["leverage"]), "symbol": q["symbol"]})
        if path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=[])
        if path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json=[{
                "algoId": 9, "clientAlgoId": "aq-x-sl1", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
                "symbol": "BTCUSDT", "side": "SELL", "algoStatus": "NEW", "triggerPrice": "59000",
                "quantity": "0", "closePosition": True}])
        if path == "/fapi/v1/ticker/24hr":
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": -1003, "msg": "Too many"})
        return httpx.Response(404, json={"code": -1, "msg": "not mocked"})


@pytest.fixture
def fake():
    return FakeBinance()


@pytest.fixture
async def client(fake):
    c = BinanceFuturesClient("demo", "KEY", "SECRET", transport=httpx.MockTransport(fake))
    await c.connect()
    yield c
    await c.close()


def test_endpoints_match_nautilus():
    assert ENDPOINTS["demo"].rest == "https://demo-fapi.binance.com"
    assert ENDPOINTS["live"].rest == "https://fapi.binance.com"
    assert ENDPOINTS["live"].ws_private.endswith("/private")


def test_parse_instruments_only_usdt_perpetual():
    insts = parse_instruments(EXCHANGE_INFO)
    assert [i.symbol for i in insts] == ["BTCUSDT"]
    i = insts[0]
    assert i.tick_size == D("0.10") and i.min_notional == D(100)
    assert i.round_price(D("60000.07")) == D("60000")
    assert i.round_qty(D("0.0019")) == D("0.001")


def test_signature_is_hmac_sha256():
    # 币安文档里的示例
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    q = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000"
         "&timestamp=1499827319559")
    assert sign(secret, q) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


async def test_limit_order_signed(client, fake):
    o = await client.place_order(OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, D("0.002"), "aq-1-in",
                                              price=D("60000.1")))
    assert o.status is OrderStatus.NEW and o.exchange_order_id == "42"
    method, path, q, headers = fake.calls[-1]
    assert path == "/fapi/v1/order" and q["timeInForce"] == "GTC" and q["price"] == "60000.1"
    assert "signature" in q and headers["x-mbx-apikey"] == "KEY"
    assert client._used_weight == 12


async def test_stop_routes_to_algo_endpoint(client, fake):
    o = await client.place_order(OrderRequest("BTCUSDT", OrderSide.SELL, OrderType.STOP_MARKET, None, "aq-1-sl1",
                                              stop_price=D("59000"), close_position=True))
    method, path, q, _ = fake.calls[-1]
    assert path == "/fapi/v1/algoOrder"
    assert q["algoType"] == "CONDITIONAL" and q["triggerPrice"] == "59000" and q["closePosition"] == "true"
    assert q["workingType"] == "MARK_PRICE" and "quantity" not in q
    assert o.exchange_order_id == "algo:7" and o.close_position
    await client.cancel_order("BTCUSDT", "aq-1-sl1")
    assert fake.calls[-1][0] == "DELETE" and fake.calls[-1][1] == "/fapi/v1/algoOrder"


async def test_open_orders_includes_algo_and_cancel_after_restart(client, fake):
    orders = await client.open_orders()
    assert len(orders) == 1 and orders[0].type is OrderType.STOP_MARKET and orders[0].stop_price == D(59000)
    await client.cancel_order("BTCUSDT", "aq-x-sl1")  # 重启后从 openAlgoOrders 认出是 Algo 单
    assert fake.calls[-1][1] == "/fapi/v1/algoOrder"


async def test_margin_type_already_isolated_ok(client):
    await client.set_margin_isolated("BTCUSDT")


async def test_rate_limit_retries_then_raises(client):
    with pytest.raises(Exception, match="429"):
        await client.tickers_24h()


async def test_order_rejection_maps_to_order_rejected(fake):
    def reject(req):
        return httpx.Response(400, json={"code": -2019, "msg": "Margin is insufficient."})

    c = BinanceFuturesClient("demo", "K", "S", transport=httpx.MockTransport(reject))
    with pytest.raises(OrderRejected, match="-2019"):
        await c.place_order(OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, D(1), "x"))
    await c.close()


def test_demo_has_no_permission_endpoint():
    import asyncio

    c = BinanceFuturesClient("demo", "K", "S")
    perms = asyncio.run(c.api_key_permissions())
    assert not perms.checked


def test_parse_position():
    p = parse_position({"symbol": "ETHUSDT", "positionAmt": "-0.500", "entryPrice": "2500.0", "leverage": "3",
                        "marginType": "isolated", "unRealizedProfit": "-1.2"})
    assert p.side is Side.SHORT and p.qty == D("0.5") and p.margin_type == "ISOLATED"
    assert parse_position({"symbol": "X", "positionAmt": "0", "entryPrice": "0"}) is None


def test_parse_order_trade_update():
    msg = json.loads("""{"e":"ORDER_TRADE_UPDATE","E":1568879465651,"T":1568879465650,"o":{
      "s":"BTCUSDT","c":"aq-abc-in","S":"BUY","o":"LIMIT","f":"GTC","q":"0.002","p":"60000","ap":"60000",
      "sp":"0","x":"TRADE","X":"FILLED","i":8886774,"l":"0.002","z":"0.002","L":"60000","n":"0.024",
      "N":"USDT","T":1568879465650,"t":1,"m":true,"R":false,"ot":"LIMIT","cp":false,"rp":"0"}}""")
    ev = parse_user_message(msg)
    assert ev.order.status is OrderStatus.FILLED and ev.last_fill_qty == D("0.002") and ev.is_maker
    assert ev.fee == D("0.024")


def test_parse_algo_update():
    msg = {"e": "ALGO_UPDATE", "E": 1750515742303, "o": {
        "caid": "aq-abc-sl1", "aid": 2148719, "at": "CONDITIONAL", "o": "STOP_MARKET", "s": "BTCUSDT",
        "S": "SELL", "ps": "BOTH", "f": "GTC", "q": "0", "X": "CANCELED", "tp": "59000", "p": "0",
        "wt": "MARK_PRICE", "pm": "NONE", "cp": True, "pP": True, "R": False, "tt": 0, "gtd": 0, "rm": ""}}
    ev = parse_user_message(msg)
    assert ev.order.status is OrderStatus.CANCELED and ev.order.exchange_order_id == "algo:2148719"
    assert ev.extra["algo"]
