"""DOM and accessibility regressions for the Quiet Authority presentation layer."""

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_HTML = (ROOT / "kr_stock_autotrader" / "decision_card_app.html").read_text(encoding="utf-8")
AUTH_HTML = (ROOT / "kr_stock_autotrader" / "ui.py").read_text(encoding="utf-8")


def _run_load_for_operation_date(summary: dict, cards: list[dict], missing: list[dict]) -> dict[str, object]:
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    draw = re.search(r"function draw\(\)\{.*?\}(?=\s*function missingDetail)", script, re.S).group(0)
    load = re.search(r"async function load\(\)\{.*?\}(?=\s*const sheetGesture)", script, re.S).group(0)
    node = f"""
const values = {{'date': {{value: '2026-08-30'}}, 'reviewable-count': {{textContent: ''}}, 'changed-count': {{textContent: ''}}, 'unverified-count': {{textContent: ''}}, 'cards': {{innerHTML: '', querySelectorAll: () => []}}, 'error': {{hidden: false, textContent: ''}}}};
const $ = selector => values[selector.slice(1)];
let summary = null, cards = [], missing = [], lastGood = [];
const calls = [];
const api = async path => {{ calls.push(path); return {{'cards/summary?operation_date=2026-08-30': {json.dumps(summary, ensure_ascii=False)}, 'cards?operation_date=2026-08-30': {json.dumps(cards, ensure_ascii=False)}, 'cards/missing?operation_date=2026-08-30': {json.dumps(missing, ensure_ascii=False)}}}[path]; }};
const invalidateDetail = () => {{}};
const resetSheetMotion = () => {{}};
const row = item => `<article>${{item.id}}</article>`;
const emptyState = () => '<article>empty</article>';
{draw}
{load}
(async () => {{ await load(); console.log(JSON.stringify({{calls, reviewable: values['reviewable-count'].textContent, changed: values['changed-count'].textContent, unverified: values['unverified-count'].textContent, rendered: values.cards.innerHTML}})); }})();
"""
    return json.loads(subprocess.check_output(["node", "-e", node], text=True))


def test_summary_strip_assigns_explicit_api_semantics_to_rendered_dom_for_operation_date():
    """Invalidation, holds, and missing evidence cannot redefine summary meanings client-side."""
    rendered = _run_load_for_operation_date(
        {"카드 생성": 9, "판단 보류": 4, "카드 미생성": 2},
        [
            {"id": "active", "invalidated_at": None, "card": {"verdict": "매수 검토 가능"}},
            {"id": "invalidated", "invalidated_at": "2026-08-30T11:00:00+09:00", "card": {"verdict": "판단 보류"}},
            {"id": "held", "invalidated_at": None, "card": {"verdict": "판단 보류"}},
        ],
        [{"id": "missing"}],
    )
    assert rendered == {
        "calls": [
            "cards/summary?operation_date=2026-08-30",
            "cards?operation_date=2026-08-30",
            "cards/missing?operation_date=2026-08-30",
        ],
        "reviewable": "9",
        "changed": "4",
        "unverified": "2",
        "rendered": "<article>active</article><article>invalidated</article><article>held</article><article>missing</article>",
    }


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_quiet_authority_buttons_are_44px_and_focus_token_meets_nontext_contrast():
    app_handle = re.search(r"\.dialog-handle\{([^}]*)\}", APP_HTML).group(1)
    auth_toggle = re.search(r"\.show-password\{([^}]*)\}", AUTH_HTML).group(1)
    for rule in (app_handle, auth_toggle):
        width = re.search(r"(?:min-)?width:(\d+)px", rule)
        height = re.search(r"(?:min-)?height:(\d+)px", rule)
        assert width is not None and int(width.group(1)) >= 44
        assert height is not None and int(height.group(1)) >= 44

    for source in (APP_HTML, AUTH_HTML):
        focus = re.search(r"focus-visible[^}]*outline:3px solid (var\(--focus\)|#[0-9a-fA-F]{6})", source).group(1)
        token = re.search(r"--focus:(#[0-9a-fA-F]{6})", source).group(1) if focus == "var(--focus)" else focus
        assert _contrast(token, "#ffffff") >= 3
        assert _contrast(token, "#f4f6f8") >= 3
