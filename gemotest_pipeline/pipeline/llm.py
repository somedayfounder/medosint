import os
import re
import google.generativeai as genai
from .blocks_config import BLOCKS, PASS_GROUPS

ORDINALS = [
    "ПЕРВЫЙ", "ВТОРОЙ", "ТРЕТИЙ", "ЧЕТВЁРТЫЙ", "ПЯТЫЙ",
    "ШЕСТОЙ", "СЕДЬМОЙ", "ВОСЬМОЙ", "ДЕВЯТЫЙ",
]


def _configure():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)


def build_prompt(analysis: str, block_ids: list | None = None, blocks: dict | None = None, cached_facts: str = "") -> str:
    """Build the multi-pass prompt for all requested blocks."""
    blocks = blocks or BLOCKS
    target = set(block_ids) if block_ids else None

    passes = []
    for i, (pass_id, pass_label) in enumerate(PASS_GROUPS):
        pass_blocks = sorted(
            [b for b in blocks.values()
             if b["pass_group"] == pass_id and (target is None or b["id"] in target)],
            key=lambda b: b["id"],
        )
        if not pass_blocks:
            continue

        ordinal = ORDINALS[i] if i < len(ORDINALS) else str(i + 1)
        lines = [f"{'='*60}", f"{ordinal} ПРОХОД — {pass_label.upper()}", f"{'='*60}", ""]

        for b in pass_blocks:
            instr = b["instructions"].format(analysis=analysis)
            lines += [
                f"БЛОК {b['id']} — {b['name']}",
                f"Формат: {b['format']}",
                "",
                instr,
                "",
                f"BLOCK: {b['id']}",
                "CONTENT:",
                "<заполни здесь>",
                "CONFIDENCE: high|medium|low",
                "VERIFY: да|нет",
                "",
            ]

        passes.append("\n".join(lines))

    facts_section = ""
    if cached_facts:
        facts_section = (
            "\n\nФАКТЫ ИЗ ВЕБА (извлечены поисковым пайплайном для этого анализа):\n"
            "Используй эти факты как дополнительный контекст при написании блоков.\n"
            f"{cached_facts}\n"
        )

    system = (
        f"Ты медицинский контент-редактор Гемотест. "
        f"Заполни все блоки для анализа: {analysis}\n\n"
        "Правила:\n"
        "— Пиши только на основе медицинских знаний\n"
        "— Если не уверен в точных числах — укажи CONFIDENCE: low и VERIFY: да\n"
        "— Таблицы форматируй в Markdown: | Колонка | Значение |\n"
        "— Не добавляй вводных фраз («В данном блоке...», «Данный анализ...»)\n"
        "— Строго соблюдай формат: BLOCK: N / CONTENT: / CONFIDENCE: / VERIFY:\n"
        "— Не пропускай ни одного блока\n\n"
        + facts_section
    )

    return system + "\n\n".join(passes)


def call_gemini(prompt: str, model: str = "gemini-1.5-flash") -> tuple[str, dict]:
    """Call Gemini, return (raw_text, usage_dict)."""
    _configure()
    m = genai.GenerativeModel(model)
    response = m.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.3,
            max_output_tokens=16384,
        ),
    )
    raw = response.text
    usage = {}
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        u = response.usage_metadata
        usage = {
            "prompt_tokens":     getattr(u, "prompt_token_count", 0),
            "completion_tokens": getattr(u, "candidates_token_count", 0),
            "total_tokens":      getattr(u, "total_token_count", 0),
        }
    return raw, usage


def parse_response(raw: str, analysis: str, block_ids: list | None = None) -> dict:
    """
    Parse LLM response into block dicts.
    Returns {block_id: {id, name, section, content, confidence, needs_verify}}.
    """
    target = set(block_ids) if block_ids else None
    results = {}

    # Split on BLOCK: markers
    pattern = re.compile(r"^BLOCK:\s*(\d+)\s*$", re.MULTILINE)
    parts = pattern.split(raw)

    # parts = [preamble, id1, text1, id2, text2, ...]
    i = 1
    while i < len(parts) - 1:
        block_id = int(parts[i].strip())
        block_text = parts[i + 1]
        i += 2

        if target and block_id not in target:
            continue
        if block_id not in BLOCKS:
            continue

        content = _extract_field(block_text, "CONTENT")
        confidence = _extract_inline(block_text, "CONFIDENCE", "medium")
        verify_raw = _extract_inline(block_text, "VERIFY", "нет")
        needs_verify = verify_raw.strip().lower() in ("да", "yes", "true", "1")

        b = BLOCKS[block_id]
        results[block_id] = {
            "id":           block_id,
            "name":         b["name"],
            "section":      b["section"],
            "pass_group":   b["pass_group"],
            "format":       b["format"],
            "content":      content.strip(),
            "confidence":   confidence.strip().lower(),
            "needs_verify": needs_verify,
            "verified":     False,
            "sources":      [],
            "warnings":     [],
            "fact_cards":   [],
            "verify_queries": [q.format(analysis=analysis) for q in b.get("verify_queries", [])],
        }

    return results


def _extract_field(text: str, field: str) -> str:
    """Extract multi-line field value (everything between field: and next KEYWORD:)."""
    m = re.search(rf"{field}:\s*\n(.*?)(?=\n(?:BLOCK|CONTENT|CONFIDENCE|VERIFY):|$)", text, re.DOTALL)
    return m.group(1) if m else ""


def _extract_inline(text: str, field: str, default: str) -> str:
    """Extract single-line field: VALUE."""
    m = re.search(rf"^{field}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else default


def estimate_cost(usage: dict, model: str = "gemini-2.0-flash") -> float:
    """Rough USD cost estimate based on Gemini pricing (per 1M tokens)."""
    prices = {
        # Flash models
        "gemini-2.0-flash":          {"in": 0.10,  "out": 0.40},
        "gemini-2.0-flash-lite":     {"in": 0.075, "out": 0.30},
        "gemini-3.5-flash-lite":     {"in": 0.075, "out": 0.30},
        "gemini-3.6-flash":          {"in": 0.10,  "out": 0.40},
        # Legacy
        "gemini-1.5-flash":          {"in": 0.075, "out": 0.30},
        "gemini-1.5-pro":            {"in": 3.50,  "out": 10.50},
    }
    # Fallback: assume flash pricing for unknown models
    p = prices.get(model, {"in": 0.10, "out": 0.40})
    inp = usage.get("prompt_tokens", 0) / 1_000_000 * p["in"]
    out = usage.get("completion_tokens", 0) / 1_000_000 * p["out"]
    return round(inp + out, 6)


# Rough API cost per single search-first block run
# Serper: ~$0.001 per query (paid plan, 3 queries = $0.003)
# Tavily: ~$0.005 per URL extraction (9 URLs = $0.045)
SERPER_COST_PER_QUERY = 0.001
TAVILY_COST_PER_URL   = 0.005


def estimate_search_cost(n_queries: int, n_urls: int) -> float:
    return round(n_queries * SERPER_COST_PER_QUERY + n_urls * TAVILY_COST_PER_URL, 6)
