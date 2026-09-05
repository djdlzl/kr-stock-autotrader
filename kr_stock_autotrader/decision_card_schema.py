"""Versioned, strict wire schema for immutable decision cards."""
from __future__ import annotations

from datetime import datetime
import math
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator

from .domain import parse_kst

SCHEMA_VERSION = 1
VERDICTS = frozenset({'매수 검토 가능', '관찰', '제외', '판단 보류'})
REQUIRED_CARD_FIELDS = frozenset({
    'schema_version', 'symbol', 'headline', 'conclusion', 'change', 'source_evidence', 'source_urls',
    'business_value', 'certainty', 'priced_in', 'filter_verdict', 'price_cap', 'window',
    'max_amount', 'max_qty', 'stop_loss', 'take_profit', 'evidence_invalidation',
    'holding_until', 'review_at', 'false_positive', 'unknowns', 'proof_point', 'next_check', 'verdict', 'confidence'
})


def _kst(value: str) -> str:
    return parse_kst(value).isoformat()


def _url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('must be an absolute http(s) URL')
    return value


class Window(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: str
    end: str

    @field_validator('start', 'end')
    @classmethod
    def kst(cls, value: str) -> str:
        return _kst(value)

    @model_validator(mode='after')
    def ordered(self):
        if parse_kst(self.start) >= parse_kst(self.end):
            raise ValueError('window start must precede end')
        return self


class TakeProfit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    price: float = Field(gt=0)
    qty: StrictInt = Field(gt=0)

    @field_validator('price', mode='before')
    @classmethod
    def numeric_price(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('must be a numeric scalar')
        return value

    @field_validator('price')
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value): raise ValueError('must be finite')
        return value


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: int | str
    source: str = Field(min_length=1)
    url: str

    @field_validator('source')
    @classmethod
    def stripped(cls, value: str) -> str:
        if not value.strip(): raise ValueError('must be nonempty')
        return value.strip()

    @field_validator('url')
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _url(value)


class DecisionCard(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    symbol: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    change: str = Field(min_length=1)
    source_evidence: list[SourceEvidence] = Field(min_length=1)
    source_urls: list[str] = Field(min_length=1)
    business_value: str = Field(min_length=1)
    certainty: str = Field(min_length=1)
    priced_in: str = Field(min_length=1)
    filter_verdict: Literal['PASS', 'FAIL']
    # Non-buy judgments may state that no evidence-supported order plan exists.
    # The model validator below keeps every one of these strict for buy review.
    price_cap: float | None = Field(default=None, gt=0)
    window: Window | dict | None = None
    max_amount: float | None = Field(default=None, gt=0)
    max_qty: StrictInt | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: list[TakeProfit] | None = None
    evidence_invalidation: dict | str | None = None
    holding_until: str | None = None
    review_at: str | None = None
    false_positive: str = Field(min_length=1)
    unknowns: str = Field(min_length=1)
    # Optional for v1 card compatibility; when supplied, these are strict source-bound v2 scenario material.
    proof_point: str | None = None
    next_check: str | None = None
    verdict: Literal['매수 검토 가능', '관찰', '제외', '판단 보류']
    confidence: float = Field(ge=0, le=1)
    valid_until: str | None = None
    order_type: Literal['limit', 'market', ''] | None = None
    split: list = Field(default_factory=list)
    expires: str | None = None

    @field_validator('symbol', 'headline', 'conclusion', 'change', 'business_value', 'certainty', 'priced_in', 'false_positive', 'unknowns')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip(): raise ValueError('must be nonempty')
        return value.strip()

    @field_validator('proof_point', 'next_check')
    @classmethod
    def optional_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip(): raise ValueError('must be nonempty when supplied')
        return value.strip() if value is not None else None

    @field_validator('source_urls')
    @classmethod
    def urls(cls, value: list[str]) -> list[str]:
        return [_url(item) for item in value]

    @field_validator('price_cap', 'max_amount', 'stop_loss', 'confidence', mode='before')
    @classmethod
    def numeric_scalars(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('must be a numeric scalar')
        return value

    @field_validator('price_cap', 'max_amount', 'stop_loss', 'confidence')
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is None:
            return value
        if not math.isfinite(value): raise ValueError('must be finite')
        return value

    @field_validator('holding_until', 'review_at', 'valid_until', 'expires')
    @classmethod
    def dates(cls, value: str | None) -> str | None:
        return _kst(value) if value not in (None, '') else value

    @model_validator(mode='after')
    def buy_review_requires_concrete_order_plan(self):
        """Keep new scenario-eligible saves complete before persistence."""
        if self.verdict in {'판단 보류', '매수 검토 가능'} and (
            not self.proof_point or not self.next_check
        ):
            raise ValueError('scenario-eligible verdict requires proof_point and next_check')
        if self.verdict != '매수 검토 가능':
            if isinstance(self.window, dict) and self.window:
                raise ValueError('non-buy window may only be null or empty')
            return self
        required = (
            self.price_cap, self.window, self.max_amount, self.max_qty, self.stop_loss,
            self.take_profit, self.evidence_invalidation, self.holding_until, self.review_at,
            self.valid_until, self.expires, self.order_type,
        )
        if not isinstance(self.window, Window) or any(value in (None, '', [], {}) for value in required) or (
            isinstance(self.evidence_invalidation, str) and not self.evidence_invalidation.strip()
        ):
            raise ValueError('buy-review requires concrete order and exit fields')
        return self


def validate_card(payload: object) -> dict:
    """Validate and normalize the persisted card contract, raising ValueError."""
    return DecisionCard.model_validate(payload).model_dump(mode='json')
