REQUIRED_CARD_FIELDS = frozenset({
    'symbol', 'headline', 'conclusion', 'change', 'source_evidence', 'source_urls',
    'business_value', 'certainty', 'priced_in', 'filter_verdict', 'price_cap', 'window',
    'max_amount', 'max_qty', 'stop_loss', 'take_profit', 'evidence_invalidation',
    'holding_until', 'review_at', 'false_positive', 'unknowns', 'verdict', 'confidence'
})
VERDICTS = frozenset({'매수 검토 가능', '관찰', '제외', '판단 보류'})
