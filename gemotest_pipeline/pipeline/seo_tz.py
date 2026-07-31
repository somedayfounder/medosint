"""
SEO TZ (Technical Specification) parser.

Reads a markdown SEO brief (H2:/H3: notation) and returns per-block
overrides: extra instructions, compliance notes, search queries derived
from the TZ topics.
"""

import re


# Map H2 title keywords → block ID
H2_BLOCK_PATTERNS = [
    (r'что показывает|что измеряет',                       32),
    (r'что такое|что представляет',                        32),
    (r'зачем сдавать|для чего.*анализ',                   46),
    (r'показания к|когда назначают',                       46),
    (r'группе риска|в зоне риска',                         12),
    (r'симптом|при каких жалобах',                          4),
    (r'избыток|токсичность|гипервитамин|чем опасен',        5),
    (r'расшифровка|интерпретац|результат',                  5),
    (r'подготов',                                           6),
    (r'как проводится|как делается|метод исследования',    34),
    (r'влияет на результат|что влияет',                     9),
    (r'дополнительн.*анализ|какие.*анализ',                15),
    (r'вопрос|faq|частые вопросы',                          7),
    (r'миф|заблуждени',                                     8),
]

COMPLIANCE_MARKERS = [
    'не утверждать', 'не использовать', 'описывать осторожно',
    'не следует', 'не связывать', 'не подтверждают', 'не менять',
    'только при наличии', 'важно:', 'нельзя', 'без медицинского',
    'по назначению врача',
]

STOPWORDS = {
    'и', 'в', 'на', 'к', 'по', 'с', 'из', 'для', 'что', 'при', 'или', 'а',
    'но', 'же', 'это', 'от', 'до', 'как', 'об', 'анализ', 'исследование',
    'помогает', 'может', 'является', 'используется', 'нужно', 'необходимо',
    'следует', 'также', 'только', 'если', 'когда', 'чтобы', 'после', 'перед',
}


def _match_block(h2_title: str) -> int | None:
    t = h2_title.lower()
    for pattern, block_id in H2_BLOCK_PATTERNS:
        if re.search(pattern, t):
            return block_id
    return None


def _is_compliance(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in COMPLIANCE_MARKERS)


def _keywords_from_bullets(bullets: list[str], n: int = 5) -> list[str]:
    words = []
    for b in bullets:
        for w in re.split(r'\W+', b.lower()):
            if w and w not in STOPWORDS and len(w) > 3:
                words.append(w)
            if len(words) >= n:
                return words
    return words


def _build_queries(h2_title: str, bullets: list[str], keywords: list[str]) -> list[str]:
    queries = []

    # From H2 title
    clean = re.sub(r'[^\w\s]', '', h2_title).strip()
    queries.append(f'{{analysis}} {clean}')

    # From bullet key terms
    key_terms = _keywords_from_bullets(bullets, 5)
    if key_terms:
        queries.append(f'{{analysis}} {" ".join(key_terms[:4])}')

    # From top SEO keyword
    if keywords:
        queries.append(keywords[0])

    return queries[:3]


def parse_seo_tz(content: str) -> dict:
    """
    Parse SEO TZ.  Returns:
    {
        "keywords":      list[str],
        "lsi_terms":     list[str],
        "block_overrides": {
            block_id: {
                "h2_titles":      list[str],
                "requirements":   list[str],
                "compliance":     list[str],
                "search_queries": list[str],
            }
        }
    }
    """
    lines = content.splitlines()
    result: dict = {"keywords": [], "lsi_terms": [], "block_overrides": {}}

    mode = None
    current_block: int | None = None
    current_h2 = ''
    current_req: list[str] = []
    current_comp: list[str] = []

    def flush():
        nonlocal current_block, current_h2, current_req, current_comp
        if current_block is None:
            return
        queries = _build_queries(current_h2, current_req, result["keywords"])
        if current_block in result["block_overrides"]:
            ex = result["block_overrides"][current_block]
            ex["h2_titles"].append(current_h2)
            ex["requirements"].extend(current_req)
            ex["compliance"].extend(current_comp)
            seen = set(ex["search_queries"])
            ex["search_queries"].extend(q for q in queries if q not in seen)
        else:
            result["block_overrides"][current_block] = {
                "h2_titles":      [current_h2],
                "requirements":   list(current_req),
                "compliance":     list(current_comp),
                "search_queries": queries,
            }
        current_block = None
        current_h2 = ''
        current_req = []
        current_comp = []

    SKIP_PREFIXES = (
        'раскрыть', 'указать', 'перечислить', 'оформить', 'описать',
        'допускается', 'обязательно', 'подчеркнуть', 'включить', 'возможные',
    )

    for raw in lines:
        line = raw.strip()
        low = line.lower()

        # ── Top-level section detection ────────────────────────────
        if re.search(r'^\d+\.\s*(основные\s*ключевые|ключевые\s*слова)', low):
            flush(); mode = 'keywords'; continue
        if re.search(r'^\d+\.\s*(lsi|тематические)', low):
            flush(); mode = 'lsi'; continue
        if re.search(r'^\d+\.\s*(рекоменду|структур)', low):
            flush(); mode = 'structure'; continue

        if mode == 'keywords':
            if line.startswith('*'):
                kw = line.lstrip('*').strip().rstrip(';')
                if kw:
                    result["keywords"].append(kw)

        elif mode == 'lsi':
            if line.startswith('*'):
                lsi = line.lstrip('*').strip().rstrip(';')
                if lsi:
                    result["lsi_terms"].append(lsi)

        elif mode == 'structure':
            # H2: Title
            m2 = re.match(r'^H2:\s*(.*)', line, re.I)
            if m2:
                flush()
                h2_title = m2.group(1).strip()
                bid = _match_block(h2_title)
                if bid:
                    current_block = bid
                    current_h2 = h2_title
                continue

            # H3: question inside FAQ section
            m3 = re.match(r'^H3:\s*(.*)', line, re.I)
            if m3 and current_block == 7:
                q = m3.group(1).strip()
                if q:
                    current_req.append(q)
                continue

            if current_block is not None:
                if line.startswith('*') or line.startswith('-'):
                    text = line.lstrip('*-').strip().rstrip(';')
                    if text:
                        if _is_compliance(text):
                            current_comp.append(text)
                        else:
                            current_req.append(text)
                elif line and not any(low.startswith(p) for p in SKIP_PREFIXES):
                    if _is_compliance(line):
                        current_comp.append(line)

    flush()
    return result


def build_lsi_snippet(lsi_terms: list[str]) -> str:
    if not lsi_terms:
        return ''
    sample = ', '.join(lsi_terms[:15])
    return (
        f'\n\nSEO-термины: используй следующие слова и синонимы естественно по тексту '
        f'(не все — только уместные): {sample}.'
    )


def apply_tz_to_block(block: dict, override: dict, lsi_terms: list[str]) -> dict:
    block = dict(block)

    parts = []
    if override.get("requirements"):
        reqs = '\n'.join(f'  — {r}' for r in override["requirements"])
        parts.append(f'ТРЕБОВАНИЯ ИЗ SEO ТЗ:\n{reqs}')
    if override.get("compliance"):
        comps = '\n'.join(f'  ⚠ {c}' for c in override["compliance"])
        parts.append(f'ОГРАНИЧЕНИЯ ИЗ SEO ТЗ:\n{comps}')

    if parts:
        block["instructions"] = block.get("instructions", "") + '\n\n' + '\n\n'.join(parts)

    # Merge search queries: TZ queries first, then existing
    if override.get("search_queries"):
        tz_q = override["search_queries"]
        existing = block.get("search_queries", [])
        seen = set(tz_q)
        merged = list(tz_q) + [q for q in existing if q not in seen]
        block["search_queries"] = merged[:5]

    # LSI terms
    lsi_snippet = build_lsi_snippet(lsi_terms)
    if lsi_snippet:
        block["instructions"] = block.get("instructions", "") + lsi_snippet

    return block


def apply_tz_to_blocks(blocks: dict, tz: dict) -> dict:
    """Return new blocks dict with TZ overrides applied."""
    lsi = tz.get("lsi_terms", [])
    overrides = tz.get("block_overrides", {})
    result = {}
    for bid, block in blocks.items():
        override = overrides.get(bid)
        if override:
            result[bid] = apply_tz_to_block(block, override, lsi)
        elif lsi:
            b = dict(block)
            b["instructions"] = b.get("instructions", "") + build_lsi_snippet(lsi)
            result[bid] = b
        else:
            result[bid] = block
    return result
