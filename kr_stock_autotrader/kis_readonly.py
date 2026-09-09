"""Deliberately narrow KIS production read-only client; no order surface exists."""
from __future__ import annotations
import os
import re
import math
import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
import httpx
from . import db as dbmod
from .domain import KST, now_kst, previous_krx_business_date
from .intraday_market_context import INTRADAY_MINUTE_PATH as _INTRADAY_MINUTE_PATH, INTRADAY_MINUTE_TR_ID as _INTRADAY_MINUTE_TR_ID, IntradayMinuteSnapshot, project_intraday_minute_snapshot

PRODUCTION_BASE_URL = "https://openapi.koreainvestment.com:9443"
OAUTH_PATH = "/oauth2/tokenP"
QUOTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
QUOTE_TR_ID = "FHKST01010100"
ORDERBOOK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
ORDERBOOK_TR_ID = "FHKST01010200"
# Actual KIS orderbook shape: output1 carries ask/bid prices and quantities;
# output2 carries stck_prpr (last/current) and stck_shrn_iscd (requested symbol).
# Official KIS contract (endpoint, TR, output2 field catalogue and sample):
# https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice
DAILY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_CHART_TR_ID = "FHKST03010100"
DAILY_CHART_OFFICIAL_REFERENCE = "https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
INTRADAY_MINUTE_PATH = _INTRADAY_MINUTE_PATH
INTRADAY_MINUTE_TR_ID = _INTRADAY_MINUTE_TR_ID
TOKEN_REFRESH_SKEW = timedelta(seconds=30)
MAX_TOKEN_LIFETIME = timedelta(hours=24)
REQUEST_START_INTERVAL_SECONDS = 0.1


class KISOAuthCacheError(RuntimeError):
    """Internal fail-closed signal for unavailable or malformed token storage."""


@dataclass(frozen=True, repr=False)
class DailySnapshot:
    """Closed internal KIS daily-chart contract; it is never an API payload."""
    summary_market_cap_100m: float
    bars: tuple[dict, ...]
    retrieved_at: datetime


def _positive_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid KIS numeric field")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("invalid KIS numeric field")
    return number

class Transport(Protocol):
    def request(self, method: str, url: str, **kwargs): ...

def _production_base(value: str | None) -> str:
    """Accept only the literal production KIS host, optionally slash-normalized."""
    candidate = (value or PRODUCTION_BASE_URL).rstrip("/")
    if candidate != PRODUCTION_BASE_URL:
        raise ValueError("KIS_BASE_URL must be the pinned production KIS host")
    return candidate

def _response_ok(response) -> bool:
    return isinstance(getattr(response, "status_code", None), int) and 200 <= response.status_code < 300

def _expiry_from(payload: dict, now: datetime) -> datetime:
    official = payload.get("access_token_token_expired")
    if isinstance(official, str) and official:
        try:
            parsed = datetime.fromisoformat(official.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=KST)
            expiry = parsed.astimezone(KST)
            if now < expiry <= now + MAX_TOKEN_LIFETIME:
                return expiry
        except ValueError:
            pass
    seconds = payload.get("expires_in")
    if isinstance(seconds, bool):
        raise ValueError("invalid expires_in")
    try:
        bounded = min(max(int(seconds), 1), int(MAX_TOKEN_LIFETIME.total_seconds()))
    except (TypeError, ValueError):
        raise ValueError("invalid expires_in")
    return now + timedelta(seconds=bounded)

class KISReadOnlyClient:
    """Only OAuth and one hard-coded domestic current-price request are callable."""
    _request_start_lock = threading.Lock()
    _last_request_started: float | None = None

    def __init__(self, app_key: str | None = None, app_secret: str | None = None, *, base_url: str | None = None, transport: Transport | None = None):
        self._app_key = app_key if app_key is not None else os.getenv("KIS_APP_KEY", "")
        self._app_secret = app_secret if app_secret is not None else os.getenv("KIS_APP_SECRET", "")
        self._base_url = _production_base(base_url if base_url is not None else os.getenv("KIS_BASE_URL"))
        self._transport = transport or httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False)
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    @staticmethod
    def readiness() -> dict:
        # Presence-only signals still require account/product-shaped values; no
        # account endpoint is called and LS_ACCOUNT is deliberately ignored.
        account = os.getenv("KIS_ACCOUNT_NO") or os.getenv("R_ACCOUNT_NUMBER")
        product = os.getenv("KIS_ACCOUNT_PRODUCT_CODE")
        account_ready = bool(re.fullmatch(r"\d{8}", account or ""))
        product_ready = bool(re.fullmatch(r"\d{2}", product or ""))
        key_present, secret_present = bool(os.getenv("KIS_APP_KEY")), bool(os.getenv("KIS_APP_SECRET"))
        return {"app_credentials": "present" if key_present and secret_present else "missing", "app_key_present": key_present, "app_secret_present": secret_present,
                "account_readiness": "ready" if account_ready and product_ready else "blocked_missing_account_env", "live_trading": False, "broker_mode": "read_only_dry_run", "order_endpoint_compiled": False,
                "network_order_calls": 0, "environment": "production"}

    def _request(self, method: str, path: str, **kwargs):
        if (method, path) not in {("POST", OAUTH_PATH), ("GET", QUOTE_PATH), ("GET", ORDERBOOK_PATH), ("GET", DAILY_CHART_PATH), ("GET", INTRADAY_MINUTE_PATH)}:
            raise ValueError("non-allowlisted KIS request")
        # The lock is process-local and spans the actual transport start so a
        # preempted caller cannot be overtaken after reserving its time slot.
        with self._request_start_lock:
            if self._last_request_started is not None:
                deadline = self._last_request_started + REQUEST_START_INTERVAL_SECONDS
                while (remaining := deadline - time.monotonic()) > 0:
                    time.sleep(remaining)
            self.__class__._last_request_started = time.monotonic()
            return self._transport.request(method, self._base_url + path, **kwargs)

    def _clear_token(self) -> None:
        self._token = None
        self._token_expiry = None

    def _invalidate_cached_token(self, rejected_token: str) -> None:
        """Remove a broker-rejected token so the retry cannot read it back."""
        cache = None
        try:
            cache = dbmod.connect()
            cache.execute("BEGIN IMMEDIATE")
            cache.execute(
                "DELETE FROM kis_oauth_token_cache WHERE cache_key=? AND access_token=?",
                (self._token_cache_key(), rejected_token),
            )
            cache.commit()
        except sqlite3.Error as exc:
            if cache is not None:
                cache.rollback()
            raise KISOAuthCacheError("KIS OAuth cache unavailable") from exc
        finally:
            if cache is not None:
                cache.close()

    def _token_cache_key(self) -> str:
        """Scope durable tokens without persisting an application credential."""
        return hashlib.sha256(self._app_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _cached_expiry(value: object) -> datetime:
        if not isinstance(value, str):
            raise KISOAuthCacheError("KIS OAuth cache unavailable")
        try:
            expiry = datetime.fromisoformat(value)
        except ValueError as exc:
            raise KISOAuthCacheError("KIS OAuth cache unavailable") from exc
        if expiry.tzinfo is None:
            raise KISOAuthCacheError("KIS OAuth cache unavailable")
        return expiry.astimezone(KST)

    def _token_value(self) -> str:
        now = now_kst()
        if self._token and self._token_expiry and now + TOKEN_REFRESH_SKEW < self._token_expiry:
            return self._token
        self._clear_token()
        cache = None
        try:
            cache = dbmod.connect()
            # This spans the refresh so another process waits, then observes
            # the committed row instead of independently issuing a token.
            cache.execute("BEGIN IMMEDIATE")
            row = cache.execute(
                "SELECT access_token, expires_at FROM kis_oauth_token_cache WHERE cache_key=?",
                (self._token_cache_key(),),
            ).fetchone()
            if row is not None:
                token, expiry = row["access_token"], self._cached_expiry(row["expires_at"])
                if isinstance(token, str) and token and now + TOKEN_REFRESH_SKEW < expiry:
                    cache.commit()
                    self._token, self._token_expiry = token, expiry
                    return token
                if not isinstance(token, str) or not token:
                    raise KISOAuthCacheError("KIS OAuth cache unavailable")
            if not self._app_key or not self._app_secret:
                raise RuntimeError("KIS app credentials missing")
            response = self._request("POST", OAUTH_PATH, json={"grant_type":"client_credentials", "appkey":self._app_key, "appsecret":self._app_secret}, headers={"content-type":"application/json"})
            if not _response_ok(response):
                raise RuntimeError("KIS OAuth HTTP failure")
            try:
                payload = response.json(); token = payload["access_token"]
                expiry = _expiry_from(payload, now)
            except (ValueError, KeyError, TypeError):
                raise RuntimeError("KIS OAuth response malformed")
            if not isinstance(token, str) or not token:
                raise RuntimeError("KIS OAuth response malformed")
            cache.execute(
                """INSERT INTO kis_oauth_token_cache(cache_key,access_token,expires_at) VALUES(?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET access_token=excluded.access_token, expires_at=excluded.expires_at""",
                (self._token_cache_key(), token, expiry.isoformat()),
            )
            cache.commit()
            self._token, self._token_expiry = token, expiry
            return token
        except KISOAuthCacheError:
            if cache is not None:
                cache.rollback()
            raise
        except sqlite3.Error as exc:
            if cache is not None:
                cache.rollback()
            raise KISOAuthCacheError("KIS OAuth cache unavailable") from exc
        except Exception:
            if cache is not None:
                cache.rollback()
            raise
        finally:
            if cache is not None:
                cache.close()

    @staticmethod
    def _auth_expired(response, payload: dict | None) -> bool:
        if getattr(response, "status_code", None) == 401:
            return True
        if not isinstance(payload, dict):
            return False
        return payload.get("msg_cd") in {"EGW00121", "EGW00123"}

    def _quote_once(self, symbol: str, token: str) -> tuple[dict, datetime, bool]:
        response = self._request("GET", QUOTE_PATH, params={"FID_COND_MRKT_DIV_CODE":"J", "FID_INPUT_ISCD":symbol}, headers={"authorization":f"Bearer {token}", "appkey":self._app_key, "appsecret":self._app_secret, "tr_id":QUOTE_TR_ID})
        # This is completion of retrieval, not an exchange trade timestamp.
        retrieved = now_kst()
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise ValueError("KIS quote malformed")
        if self._auth_expired(response, payload):
            return {}, retrieved, True
        if not _response_ok(response) or payload.get("rt_cd") != "0":
            raise ValueError("KIS quote unavailable")
        output = payload["output"]
        price, volume = float(output["stck_prpr"]), float(output["acml_vol"])
        if price <= 0 or volume < 0:
            raise ValueError("KIS quote malformed")
        return {"price": price, "volume": volume}, retrieved, False

    def current_price(self, symbol: str) -> dict:
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("symbol must be six digits")
        retrieved = now_kst()
        try:
            for attempt in range(2):
                token = self._token_value()
                quote, retrieved, auth_expired = self._quote_once(symbol, token)
                if not auth_expired:
                    return {"symbol":symbol, **quote, "quote_known_at":retrieved.isoformat(), "retrieved_at":retrieved.isoformat(), "timestamp_source":"network_retrieved_at", "source":"KIS", "environment":"production", "status":"ok"}
                self._clear_token()
                self._invalidate_cached_token(token)
                if attempt == 1:
                    raise ValueError("KIS quote authentication failed")
        except (RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError):
            pass
        return {"symbol":symbol,"source":"KIS","environment":"production","status":"unavailable","retrieved_at":retrieved.isoformat(),"timestamp_source":"network_retrieved_at"}

    def _orderbook_once(self, symbol: str, token: str) -> tuple[dict, datetime, bool]:
        response = self._request("GET", ORDERBOOK_PATH, params={"FID_COND_MRKT_DIV_CODE":"J", "FID_INPUT_ISCD":symbol}, headers={"authorization":f"Bearer {token}", "appkey":self._app_key, "appsecret":self._app_secret, "tr_id":ORDERBOOK_TR_ID})
        retrieved = now_kst()
        try: payload = response.json()
        except (ValueError, TypeError): raise ValueError("KIS orderbook malformed")
        if self._auth_expired(response, payload): return {}, retrieved, True
        if not _response_ok(response) or payload.get("rt_cd") != "0":
            raise ValueError("KIS orderbook unavailable")
        output1, output2 = payload.get("output1"), payload.get("output2")
        if not isinstance(output1, dict) or not isinstance(output2, dict):
            raise ValueError("KIS orderbook unavailable")
        if output2.get("stck_shrn_iscd") != symbol:
            raise ValueError("KIS orderbook symbol mismatch")
        # Deliberately closed allowlist: no raw KIS output leaves this method.
        last = _positive_number(output2["stck_prpr"])
        ask, bid, ask_qty, bid_qty = (_positive_number(output1[k]) for k in ("askp1", "bidp1", "askp_rsqn1", "bidp_rsqn1"))
        if bid > ask: raise ValueError("KIS orderbook malformed")
        return {"last_price":last, "best_bid":bid, "best_ask":ask, "top_bid_qty":bid_qty, "top_ask_qty":ask_qty}, retrieved, False

    def orderbook(self, symbol: str) -> dict:
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol): raise ValueError("symbol must be six digits")
        retrieved = now_kst()
        try:
            for attempt in range(2):
                token = self._token_value()
                book, retrieved, expired = self._orderbook_once(symbol, token)
                if not expired:
                    return {"symbol":symbol, **book, "quote_known_at":retrieved.isoformat(), "retrieved_at":retrieved.isoformat(), "timestamp_source":"network_retrieved_at", "source":"KIS", "environment":"production", "status":"ok"}
                self._clear_token()
                self._invalidate_cached_token(token)
                if attempt == 1: raise ValueError("KIS orderbook authentication failed")
        except (RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError): pass
        return {"symbol":symbol, "source":"KIS", "environment":"production", "status":"unavailable", "retrieved_at":retrieved.isoformat(), "timestamp_source":"network_retrieved_at"}

    @staticmethod
    def project_orderbook(raw: dict, symbol: str) -> dict:
        """Project synthetic/raw-shaped data into the safe top-of-book schema."""
        if not isinstance(raw, dict) or raw.get("symbol", symbol) != symbol: raise ValueError("orderbook symbol mismatch")
        try: values = [float(raw[k]) for k in ("last", "bid", "ask", "bid_qty", "ask_qty")]
        except (KeyError, TypeError, ValueError): raise ValueError("malformed orderbook")
        if not all(math.isfinite(x) and x > 0 for x in values) or values[1] > values[2]: raise ValueError("malformed orderbook")
        last,bid,ask,bq,aq = values; stamp=now_kst().isoformat()
        return {"symbol":symbol,"last_price":last,"best_bid":bid,"best_ask":ask,"top_bid_qty":bq,"top_ask_qty":aq,"spread_pct":(ask-bid)/last*100,"imbalance":(bq-aq)/(bq+aq),"quote_known_at":stamp,"retrieved_at":stamp,"timestamp_source":"synthetic","source":"synthetic","status":"ok"}

    def daily_snapshot(self, symbol: str, as_of: datetime) -> DailySnapshot:
        """Read the official output1 summary plus output2 bars as a closed object."""
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol) or as_of.tzinfo is None:
            raise ValueError("invalid daily-bar request")
        # A premarket request must ask only through the prior KRX business date;
        # KIS may otherwise include a current-session row even before open.
        end = previous_krx_business_date(as_of.astimezone(KST).date()).strftime("%Y%m%d")
        # At least 45 calendar days are needed for the declared 20-session
        # denominator after holidays/mismatched trading calendars; use 60.
        start = (as_of.astimezone(KST) - timedelta(days=60)).strftime("%Y%m%d")
        try:
            for attempt in range(2):
                token = self._token_value()
                response = self._request("GET", DAILY_CHART_PATH, params={"FID_COND_MRKT_DIV_CODE":"J", "FID_INPUT_ISCD":symbol, "FID_INPUT_DATE_1":start, "FID_INPUT_DATE_2":end, "FID_PERIOD_DIV_CODE":"D", "FID_ORG_ADJ_PRC":"0"}, headers={"authorization":f"Bearer {token}", "appkey":self._app_key, "appsecret":self._app_secret, "tr_id":DAILY_CHART_TR_ID})
                payload = response.json()
                if self._auth_expired(response, payload):
                    self._clear_token()
                    self._invalidate_cached_token(token)
                    if attempt == 1:
                        raise ValueError("KIS daily snapshot unavailable")
                    continue
                output1, output2 = payload.get("output1"), payload.get("output2")
                if not _response_ok(response) or payload.get("rt_cd") != "0" or not isinstance(output1, dict) or not isinstance(output2, list):
                    raise ValueError("KIS daily snapshot unavailable")
                # KIS documents hts_avls in output1, not in daily output2 bars.
                cap = _positive_number(output1.get("hts_avls"))
                if not all(isinstance(row, dict) for row in output2):
                    raise ValueError("KIS daily snapshot unavailable")
                return DailySnapshot(summary_market_cap_100m=cap, bars=tuple(output2), retrieved_at=now_kst())
        except (TypeError, ValueError, KeyError):
            raise ValueError("KIS daily snapshot unavailable")
        raise ValueError("KIS daily snapshot unavailable")

    def _intraday_once(self, symbol: str, requested_as_of: datetime, token: str) -> tuple[object, datetime, bool]:
        response = self._request(
            "GET",
            INTRADAY_MINUTE_PATH,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": requested_as_of.strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            },
            headers={"authorization": f"Bearer {token}", "appkey": self._app_key, "appsecret": self._app_secret, "tr_id": INTRADAY_MINUTE_TR_ID},
        )
        retrieved = now_kst()
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise ValueError("intraday snapshot unavailable")
        if self._auth_expired(response, payload):
            return {}, retrieved, True
        if not _response_ok(response) or payload.get("rt_cd") != "0":
            raise ValueError("intraday snapshot unavailable")
        return payload, retrieved, False

    def intraday_minute_bars(self, symbol: str, requested_as_of: datetime) -> IntradayMinuteSnapshot:
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol) or requested_as_of.tzinfo is None:
            raise ValueError("intraday snapshot unavailable")
        attempt_started = now_kst()
        if requested_as_of.astimezone(KST) > attempt_started:
            raise ValueError("intraday snapshot unavailable")
        try:
            for attempt in range(2):
                token = self._token_value()
                payload, retrieved, auth_expired = self._intraday_once(symbol, requested_as_of.astimezone(KST), token)
                if auth_expired:
                    self._clear_token()
                    self._invalidate_cached_token(token)
                    if attempt == 1:
                        raise ValueError("intraday snapshot unavailable")
                    continue
                if requested_as_of.astimezone(KST) > retrieved:
                    raise ValueError("intraday snapshot unavailable")
                return project_intraday_minute_snapshot(payload, symbol=symbol, requested_as_of=requested_as_of.astimezone(KST), retrieved_at=retrieved, provider="KIS", tr_id=INTRADAY_MINUTE_TR_ID)
        except (RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError):
            pass
        raise ValueError("intraday snapshot unavailable")
