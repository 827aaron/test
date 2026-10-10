"""币安 U 本位永续合约客户端（REST + WebSocket）。

- 环境：demo（Demo Trading，demo-fapi.binance.com）/ testnet（旧测试网）/ live。
  地址与 NautilusTrader 1.231 的 `adapters/binance/common/urls.py` 保持一致。
- 2025-12-09 起，STOP_MARKET / TAKE_PROFIT_MARKET 等条件单必须走 Algo 接口
  （POST /fapi/v1/algoOrder），旧的 /fapi/v1/order 会返回 -4120。本客户端自动分流。
- 频率限制：读取 X-MBX-USED-WEIGHT-1M，接近上限时主动等待；429/418 退避重试。

⚠️ 云端开发环境连不上币安，本文件的请求/响应格式按官方文档和 NautilusTrader 的实现编写，
   用录制的样例做单元测试；需要在 Mac mini 上用 Demo Trading 实测（见 docs/SETUP.md）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from engine.core.types import (
    AccountBalance,
    ExchangeOrder,
    ExchangePosition,
    FundingRate,
    Instrument,
    KeyPermissions,
    Kline,
    Liquidation,
    OpenInterest,
    OrderBook,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Side,
    Ticker24h,
    UserEvent,
)
from engine.exchange.base import ExchangeDisconnected, ExchangeError, OrderRejected

log = logging.getLogger(__name__)

D = Decimal


@dataclass(frozen=True)
class BinanceEndpoints:
    rest: str
    ws_market: str  # 行情流（K 线、标记价格、爆仓）
    ws_private: str  # 用户数据流
    spot_rest: str | None  # 用于 Key 权限检查（只有实盘有）


ENDPOINTS: dict[str, BinanceEndpoints] = {
    "demo": BinanceEndpoints("https://demo-fapi.binance.com", "wss://demo-fstream.binance.com",
                             "wss://demo-fstream.binance.com", None),
    "testnet": BinanceEndpoints("https://testnet.binancefuture.com", "wss://stream.binancefuture.com",
                                "wss://stream.binancefuture.com", None),
    "live": BinanceEndpoints("https://fapi.binance.com", "wss://fstream.binance.com/market",
                             "wss://fstream.binance.com/private", "https://api.binance.com"),
}

CONDITIONAL_TYPES = {OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET}
ALGO_PREFIX = "algo:"
_ALGO_STATUS = {
    "NEW": OrderStatus.NEW, "TRIGGERING": OrderStatus.NEW, "TRIGGERED": OrderStatus.FILLED,
    "FINISHED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.EXPIRED, "REJECTED": OrderStatus.REJECTED,
}


class BinanceAPIError(ExchangeError):
    def __init__(self, status: int, code: int | None, msg: str) -> None:
        super().__init__(f"HTTP {status} code={code} {msg}")
        self.status, self.code, self.msg = status, code, msg


def _fmt(x: Decimal) -> str:
    return format(x.normalize(), "f")


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


# ---------------- 解析函数（纯函数，方便用录制数据测试） ----------------

def parse_instruments(info: dict[str, Any]) -> list[Instrument]:
    out = []
    for s in info["symbols"]:
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
            continue
        f = {flt["filterType"]: flt for flt in s["filters"]}
        out.append(Instrument(
            symbol=s["symbol"], base_asset=s["baseAsset"], quote_asset=s["quoteAsset"],
            tick_size=D(f["PRICE_FILTER"]["tickSize"]), step_size=D(f["LOT_SIZE"]["stepSize"]),
            min_qty=D(f["LOT_SIZE"]["minQty"]),
            min_notional=D(f.get("MIN_NOTIONAL", {}).get("notional", "5")),
            onboard_ms=int(s.get("onboardDate", 0)), trading=s.get("status") == "TRADING",
        ))
    return out


def parse_ticker(t: dict[str, Any]) -> Ticker24h:
    return Ticker24h(t["symbol"], D(t["lastPrice"]), D(t["quoteVolume"]), D(t["priceChangePercent"]),
                     D(t["highPrice"]), D(t["lowPrice"]))


def parse_kline(k: list[Any]) -> Kline:
    return Kline(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]),
                 float(k[7]), int(k[6]))


def parse_order(o: dict[str, Any]) -> ExchangeOrder:
    return ExchangeOrder(
        symbol=o["symbol"], client_order_id=o["clientOrderId"], exchange_order_id=str(o["orderId"]),
        side=OrderSide(o["side"]), type=OrderType(o.get("origType") or o["type"]),
        status=OrderStatus(o["status"]), qty=D(o["origQty"]), filled_qty=D(o.get("executedQty", "0")),
        avg_price=D(o.get("avgPrice") or "0"),
        price=D(o["price"]) if D(o.get("price") or "0") > 0 else None,
        stop_price=D(o["stopPrice"]) if D(o.get("stopPrice") or "0") > 0 else None,
        reduce_only=bool(o.get("reduceOnly")), close_position=bool(o.get("closePosition")),
        update_ms=int(o.get("updateTime", 0)),
    )


def parse_algo_order(o: dict[str, Any]) -> ExchangeOrder:
    return ExchangeOrder(
        symbol=o["symbol"], client_order_id=o["clientAlgoId"],
        exchange_order_id=f"{ALGO_PREFIX}{o['algoId']}", side=OrderSide(o["side"]),
        type=OrderType(o["orderType"]), status=_ALGO_STATUS.get(o.get("algoStatus", "NEW"),
                                                                OrderStatus.NEW),
        qty=D(o.get("quantity") or "0"), stop_price=D(o["triggerPrice"]) if o.get("triggerPrice") else None,
        reduce_only=bool(o.get("reduceOnly")), close_position=bool(o.get("closePosition")),
        update_ms=int(o.get("updateTime") or o.get("createTime") or 0),
    )


def parse_position(p: dict[str, Any]) -> ExchangePosition | None:
    amt = D(p["positionAmt"])
    if amt == 0:
        return None
    return ExchangePosition(
        symbol=p["symbol"], side=Side.LONG if amt > 0 else Side.SHORT, qty=abs(amt),
        entry_price=D(p["entryPrice"]), leverage=int(p.get("leverage", 0) or 0),
        margin_type="ISOLATED" if str(p.get("marginType", "")).lower() == "isolated" else "CROSSED",
        unrealized_pnl=D(p.get("unRealizedProfit") or p.get("unrealizedProfit") or "0"),
    )


def parse_user_message(msg: dict[str, Any]) -> UserEvent | None:
    e = msg.get("e")
    if e == "ORDER_TRADE_UPDATE":
        o = msg["o"]
        order = ExchangeOrder(
            symbol=o["s"], client_order_id=o["c"], exchange_order_id=str(o["i"]), side=OrderSide(o["S"]),
            type=OrderType(o.get("ot") or o["o"]), status=OrderStatus(o["X"]), qty=D(o["q"]),
            filled_qty=D(o["z"]), avg_price=D(o["ap"]),
            price=D(o["p"]) if D(o["p"]) > 0 else None,
            stop_price=D(o["sp"]) if D(o.get("sp", "0")) > 0 else None,
            reduce_only=bool(o.get("R")), close_position=bool(o.get("cp")), update_ms=int(o["T"]),
        )
        return UserEvent("order", order=order, last_fill_qty=D(o["l"]), last_fill_price=D(o["L"]),
                         fee=D(o.get("n", "0")), realized_pnl=D(o.get("rp", "0")),
                         is_maker=bool(o.get("m")), ts_ms=int(msg["E"]))
    if e == "ALGO_UPDATE":
        o = msg["o"]
        order = ExchangeOrder(
            symbol=o["s"], client_order_id=o["caid"], exchange_order_id=f"{ALGO_PREFIX}{o['aid']}",
            side=OrderSide(o["S"]), type=OrderType(o["o"]),
            status=_ALGO_STATUS.get(o["X"], OrderStatus.NEW), qty=D(o.get("q") or "0"),
            stop_price=D(o["tp"]) if D(o.get("tp") or "0") > 0 else None,
            reduce_only=bool(o.get("R")), close_position=bool(o.get("cp")), update_ms=int(msg["E"]),
        )
        return UserEvent("order", order=order, ts_ms=int(msg["E"]),
                         extra={"algo": True, "reason": o.get("rm", "")})
    if e == "ACCOUNT_UPDATE":
        return UserEvent("account", ts_ms=int(msg["E"]), extra=msg.get("a", {}))
    if e == "listenKeyExpired":
        return UserEvent("account", ts_ms=int(msg.get("E", 0)), extra={"listen_key_expired": True})
    return None


# ---------------- 客户端 ----------------

class BinanceFuturesClient:
    WEIGHT_SOFT_LIMIT = 1800  # 每分钟 2400，留余量

    def __init__(self, environment: str, api_key: str | None, api_secret: str | None,
                 recv_window_ms: int = 5000, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0) -> None:
        if environment not in ENDPOINTS:
            raise ValueError(f"unknown environment {environment}")
        self.environment = environment
        self.ep = ENDPOINTS[environment]
        self._key, self._secret = api_key, api_secret
        self.recv_window = recv_window_ms
        self._http = httpx.AsyncClient(base_url=self.ep.rest, timeout=timeout, transport=transport)
        self._spot = (httpx.AsyncClient(base_url=self.ep.spot_rest, timeout=timeout, transport=transport)
                      if self.ep.spot_rest else None)
        self._time_offset = 0
        self._used_weight = 0
        self._weight_reset_at = 0.0
        self._algo_ids: dict[str, int] = {}  # clientAlgoId -> algoId
        self._lock = asyncio.Lock()

    # ----- 基础 -----
    async def connect(self) -> None:
        await self.sync_time()

    async def close(self) -> None:
        await self._http.aclose()
        if self._spot:
            await self._spot.aclose()

    async def sync_time(self) -> None:
        data = await self._request("GET", "/fapi/v1/time")
        self._time_offset = int(data["serverTime"]) - int(time.time() * 1000)

    async def _throttle(self) -> None:
        now = time.monotonic()
        if now >= self._weight_reset_at:
            self._used_weight, self._weight_reset_at = 0, now + 60
        if self._used_weight >= self.WEIGHT_SOFT_LIMIT:
            wait = self._weight_reset_at - now
            log.warning("接近币安频率上限（已用 %s），等待 %.1fs", self._used_weight, wait)
            await asyncio.sleep(max(wait, 0))

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                       signed: bool = False, spot: bool = False, retries: int = 3) -> Any:
        client = self._spot if spot else self._http
        assert client is not None
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        for attempt in range(retries + 1):
            await self._throttle()
            q = dict(params)
            if signed:
                if not (self._key and self._secret):
                    raise ExchangeError("缺少币安 API Key / Secret")
                q["timestamp"] = int(time.time() * 1000) + self._time_offset
                q["recvWindow"] = self.recv_window
                qs = urlencode(q)
                qs += "&signature=" + sign(self._secret, qs)
                headers["X-MBX-APIKEY"] = self._key
            else:
                qs = urlencode(q)
                if self._key:
                    headers["X-MBX-APIKEY"] = self._key
            url = f"{path}?{qs}" if qs else path
            try:
                r = await client.request(method, url, headers=headers)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt < retries:
                    await asyncio.sleep(0.5 * 2 ** attempt)
                    continue
                raise ExchangeDisconnected(str(e)) from e
            w = r.headers.get("X-MBX-USED-WEIGHT-1M")
            if w:
                self._used_weight = int(w)
            if r.status_code in (429, 418):
                retry_after = float(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                log.warning("币安频率限制 HTTP %s，等待 %ss", r.status_code, retry_after)
                if attempt < retries:
                    await asyncio.sleep(retry_after)
                    continue
            if r.status_code >= 500 and attempt < retries:
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            body = r.json() if r.content else {}
            if r.status_code >= 400:
                code = body.get("code") if isinstance(body, dict) else None
                msg = body.get("msg", r.text) if isinstance(body, dict) else r.text
                if code == -1021 and attempt < retries:  # 时间戳偏差
                    await self.sync_time()
                    continue
                if method in ("POST", "DELETE") and path.endswith(("order", "algoOrder")):
                    raise OrderRejected(f"code={code} {msg}")
                raise BinanceAPIError(r.status_code, code, msg)
            return body
        raise ExchangeError("unreachable")

    # ----- 行情 -----
    async def load_instruments(self) -> list[Instrument]:
        return parse_instruments(await self._request("GET", "/fapi/v1/exchangeInfo"))

    async def tickers_24h(self) -> list[Ticker24h]:
        return [parse_ticker(t) for t in await self._request("GET", "/fapi/v1/ticker/24hr")]

    async def order_book(self, symbol: str, limit: int = 100) -> OrderBook:
        d = await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        return OrderBook(symbol, [(D(p), D(q)) for p, q in d["bids"]], [(D(p), D(q)) for p, q in d["asks"]])

    async def funding_rates(self) -> list[FundingRate]:
        data = await self._request("GET", "/fapi/v1/premiumIndex")
        return [FundingRate(d["symbol"], D(d["lastFundingRate"] or "0"), D(d["markPrice"]),
                            int(d.get("nextFundingTime", 0))) for d in data]

    async def mark_price(self, symbol: str) -> Decimal:
        d = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return D(d["markPrice"])

    async def open_interest(self, symbol: str) -> OpenInterest:
        d = await self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})
        return OpenInterest(symbol, D(d["openInterest"]), int(d["time"]))

    async def open_interest_hist(self, symbol: str, period: str, limit: int) -> list[OpenInterest]:
        d = await self._request("GET", "/futures/data/openInterestHist",
                                {"symbol": symbol, "period": period, "limit": limit})
        return [OpenInterest(symbol, D(x["sumOpenInterest"]), int(x["timestamp"])) for x in d]

    async def klines(self, symbol: str, interval: str, limit: int) -> list[Kline]:
        d = await self._request("GET", "/fapi/v1/klines",
                                {"symbol": symbol, "interval": interval, "limit": limit})
        now = int(time.time() * 1000) + self._time_offset
        out = [parse_kline(k) for k in d]
        # 最后一根如果还没收盘，标记出来，信号只用已收盘的 K 线
        if out and out[-1].close_ms > now:
            k = out[-1]
            out[-1] = Kline(k.open_ms, k.open, k.high, k.low, k.close, k.volume, k.quote_volume,
                            k.close_ms, closed=False)
        return out

    async def _ws_stream(self, url: str) -> AsyncIterator[dict[str, Any]]:
        import websockets

        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
            async for raw in ws:
                yield json.loads(raw)

    async def liquidations(self) -> AsyncIterator[Liquidation]:
        async for m in self._ws_stream(f"{self.ep.ws_market}/ws/!forceOrder@arr"):
            o = m.get("o", {})
            if not o:
                continue
            yield Liquidation(o["s"], OrderSide(o["S"]), D(o["ap"] or o["p"]), D(o["z"] or o["q"]),
                              int(o["T"]))

    async def mark_prices(self) -> AsyncIterator[dict[str, Decimal]]:
        """全市场标记价格，每秒一次。"""
        async for m in self._ws_stream(f"{self.ep.ws_market}/ws/!markPrice@arr@1s"):
            if isinstance(m, list):
                yield {x["s"]: D(x["p"]) for x in m}

    # ----- 账户 -----
    async def balance(self) -> AccountBalance:
        d = await self._request("GET", "/fapi/v2/account", signed=True)
        return AccountBalance(equity=D(d["totalMarginBalance"]), wallet=D(d["totalWalletBalance"]),
                              available=D(d["availableBalance"]), unrealized=D(d["totalUnrealizedProfit"]))

    async def positions(self) -> list[ExchangePosition]:
        d = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return [p for p in (parse_position(x) for x in d) if p is not None]

    async def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        normal = await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
        algo = await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        orders = [parse_order(o) for o in normal]
        for a in algo if isinstance(algo, list) else algo.get("orders", []):
            self._algo_ids[a["clientAlgoId"]] = int(a["algoId"])
            orders.append(parse_algo_order(a))
        return orders

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage},
                            signed=True)

    async def set_margin_isolated(self, symbol: str) -> None:
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"},
                                signed=True)
        except BinanceAPIError as e:
            if e.code != -4046:  # "No need to change margin type."
                raise

    async def place_order(self, req: OrderRequest) -> ExchangeOrder:
        if req.type in CONDITIONAL_TYPES:
            return await self._place_algo(req)
        p: dict[str, Any] = {
            "symbol": req.symbol, "side": req.side.value, "type": req.type.value,
            "newClientOrderId": req.client_order_id, "newOrderRespType": "RESULT",
        }
        if req.qty is not None:
            p["quantity"] = _fmt(req.qty)
        if req.reduce_only:
            p["reduceOnly"] = "true"
        if req.type is OrderType.LIMIT:
            assert req.price is not None
            p["price"] = _fmt(req.price)
            if req.post_only:
                p["timeInForce"] = "GTX"
            elif req.good_till_ms is not None:
                p["timeInForce"] = "GTD"
                p["goodTillDate"] = req.good_till_ms
            else:
                p["timeInForce"] = "GTC"
        return parse_order(await self._request("POST", "/fapi/v1/order", p, signed=True))

    async def _place_algo(self, req: OrderRequest) -> ExchangeOrder:
        assert req.stop_price is not None
        p: dict[str, Any] = {
            "algoType": "CONDITIONAL", "symbol": req.symbol, "side": req.side.value,
            "type": req.type.value, "triggerPrice": _fmt(req.stop_price),
            "workingType": "MARK_PRICE", "priceProtect": "TRUE", "clientAlgoId": req.client_order_id,
        }
        if req.close_position:
            p["closePosition"] = "true"
        else:
            assert req.qty is not None
            p["quantity"] = _fmt(req.qty)
            p["reduceOnly"] = "true" if req.reduce_only else None
        d = await self._request("POST", "/fapi/v1/algoOrder", p, signed=True)
        self._algo_ids[d["clientAlgoId"]] = int(d["algoId"])
        return parse_algo_order(d)

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        if client_order_id in self._algo_ids:
            await self._request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id},
                                signed=True)
            self._algo_ids.pop(client_order_id, None)
            return
        await self._request("DELETE", "/fapi/v1/order",
                            {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True)

    async def api_key_permissions(self) -> KeyPermissions:
        if self._spot is None:
            # Demo / 测试网没有这个接口，只能请用户自己确认
            return KeyPermissions(can_trade_futures=True, can_withdraw=False, ip_restricted=False,
                                  checked=False)
        d = await self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True, spot=True)
        return KeyPermissions(can_trade_futures=bool(d.get("enableFutures")),
                              can_withdraw=bool(d.get("enableWithdrawals")),
                              ip_restricted=bool(d.get("ipRestrict")))

    # ----- 用户数据流 -----
    async def _listen_key(self) -> str:
        d = await self._request("POST", "/fapi/v1/listenKey")
        return d["listenKey"]

    async def user_events(self) -> AsyncIterator[UserEvent]:
        key = await self._listen_key()

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(30 * 60)
                try:
                    await self._request("PUT", "/fapi/v1/listenKey")
                except Exception:
                    log.exception("listenKey 续期失败")

        ka = asyncio.create_task(keepalive())
        base = self.ep.ws_private.rstrip("/")
        url = f"{base}/ws?listenKey={key}"
        try:
            async for m in self._ws_stream(url):
                ev = parse_user_message(m)
                if ev is not None:
                    yield ev
                    if ev.extra.get("listen_key_expired"):
                        raise ExchangeDisconnected("listenKey expired")
        finally:
            ka.cancel()
