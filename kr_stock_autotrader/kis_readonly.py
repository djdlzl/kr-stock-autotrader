"""Deliberately narrow KIS production read-only client; no order surface exists."""
from __future__ import annotations
import os, re
from datetime import datetime
from typing import Protocol
import httpx
from .domain import KST, now_kst

PRODUCTION_BASE_URL = "https://openapi.koreainvestment.com:9443"
OAUTH_PATH = "/oauth2/tokenP"
QUOTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
QUOTE_TR_ID = "FHKST01010100"

class Transport(Protocol):
    def request(self, method: str, url: str, **kwargs): ...

class KISReadOnlyClient:
    """Only OAuth and one hard-coded domestic current-price request are callable."""
    def __init__(self, app_key: str | None = None, app_secret: str | None = None, *, base_url: str | None = None, transport: Transport | None = None):
        self._app_key = app_key if app_key is not None else os.getenv("KIS_APP_KEY", "")
        self._app_secret = app_secret if app_secret is not None else os.getenv("KIS_APP_SECRET", "")
        self._base_url = (base_url or os.getenv("KIS_BASE_URL") or PRODUCTION_BASE_URL).rstrip("/")
        if not self._base_url.startswith("https://"):
            raise ValueError("KIS_BASE_URL must be https")
        self._transport = transport or httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False)
        self._token: str | None = None

    @staticmethod
    def readiness() -> dict:
        # R_ACCOUNT_NUMBER is a verified legacy 8-digit account-number alias; LS_ACCOUNT is deliberately ignored.
        account = bool(os.getenv("KIS_ACCOUNT_NO") or os.getenv("R_ACCOUNT_NUMBER")) and bool(os.getenv("KIS_ACCOUNT_PRODUCT_CODE"))
        key_present, secret_present = bool(os.getenv("KIS_APP_KEY")), bool(os.getenv("KIS_APP_SECRET"))
        return {"app_credentials": "present" if key_present and secret_present else "missing",
                "app_key_present": key_present, "app_secret_present": secret_present,
                "account_readiness": "ready" if account else "blocked_missing_account_env",
                "live_trading": False, "broker_mode": "read_only_dry_run", "order_endpoint_compiled": False,
                "network_order_calls": 0, "environment": "production"}

    def _request(self, method: str, path: str, **kwargs):
        # Private guard remains enforceable even for test doubles.
        if (method, path) not in {("POST", OAUTH_PATH), ("GET", QUOTE_PATH)}:
            raise ValueError("non-allowlisted KIS request")
        return self._transport.request(method, self._base_url + path, **kwargs)

    def _token_value(self) -> str:
        if self._token: return self._token
        if not self._app_key or not self._app_secret: raise RuntimeError("KIS app credentials missing")
        response = self._request("POST", OAUTH_PATH, json={"grant_type":"client_credentials", "appkey":self._app_key, "appsecret":self._app_secret}, headers={"content-type":"application/json"})
        try:
            payload = response.json(); token = payload["access_token"]
        except (ValueError, KeyError, TypeError): raise RuntimeError("KIS OAuth response malformed")
        if not isinstance(token, str) or not token: raise RuntimeError("KIS OAuth response malformed")
        self._token = token
        return token

    def current_price(self, symbol: str) -> dict:
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol): raise ValueError("symbol must be six digits")
        retrieved = now_kst()
        try:
            token = self._token_value()
            response = self._request("GET", QUOTE_PATH, params={"FID_COND_MRKT_DIV_CODE":"J", "FID_INPUT_ISCD":symbol}, headers={"authorization":f"Bearer {token}", "appkey":self._app_key, "appsecret":self._app_secret, "tr_id":QUOTE_TR_ID})
            payload = response.json(); output = payload["output"]
            if payload.get("rt_cd") != "0": raise ValueError
            price = float(output["stck_prpr"]); volume = float(output["acml_vol"])
            stamp = output.get("stck_cntg_hour")
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{6}", stamp): raise ValueError
            known = retrieved.replace(hour=int(stamp[:2]), minute=int(stamp[2:4]), second=int(stamp[4:]), microsecond=0).astimezone(KST)
            if price <= 0 or volume < 0: raise ValueError
        except (RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError):
            return {"symbol":symbol,"source":"KIS","environment":"production","status":"unavailable","retrieved_at":retrieved.isoformat()}
        return {"symbol":symbol,"price":price,"volume":volume,"quote_known_at":known.isoformat(),"retrieved_at":retrieved.isoformat(),"source":"KIS","environment":"production","status":"ok"}
