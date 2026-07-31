import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .llm import build_prompt, call_gemini, parse_response, estimate_cost
from .blocks_config import BLOCKS
from .search import search_and_extract


def slugify(s: str) -> str:
    cyr = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z',
        'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
        'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh',
        'щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }
    s = s.lower()
    s = ''.join(cyr.get(c, c) for c in s)
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')


# Blocks that use search-first pipeline instead of raw LLM
SEARCH_FIRST_BLOCKS = {44}

# Models for search-first pipeline steps (fixed, independent of UI model selection)
EXTRACT_MODEL  = "gemini-3.5-flash-lite"   # reads raw Tavily HTML, pulls out facts
GENERATE_MODEL = "gemini-3.6-flash"         # writes the final block from clean facts


def _extract_facts_from_sources(analysis: str, block: dict, sources: list[dict]) -> tuple[str, dict]:
    """Lite model: read raw Tavily content, extract only relevant facts for this block."""
    raw_pages = "\n\n---\n\n".join(
        f"URL: {s['url']}\nЗаголовок: {s['title']}\n\n{s['content']}"
        for s in sources
    )
    prompt = (
        f"Тебе дан сырой текст нескольких веб-страниц об анализе «{analysis}».\n"
        f"Нужно заполнить блок «{block['name']}».\n"
        f"Описание блока: {block['description']}\n\n"
        f"Из текста ниже извлеки ТОЛЬКО факты, релевантные для этого блока.\n"
        f"Убери навигацию, рекламу, повторы, нерелевантные разделы.\n"
        f"Верни сжатый список фактов — по одному на строку.\n\n"
        f"Страницы:\n{raw_pages}"
    )
    return call_gemini(prompt, EXTRACT_MODEL)


def _generate_block_from_facts(analysis: str, block: dict, facts: str, sources: list[dict], model: str) -> tuple[str, dict]:
    """Strong model: write the block from extracted facts."""
    source_list = "\n".join(f"- {s['url']}" for s in sources)
    instructions = block["instructions"].format(analysis=analysis)
    prompt = (
        f"Ты медицинский контент-редактор Гемотест.\n"
        f"Анализ: {analysis}\n"
        f"Блок: {block['name']}\n\n"
        f"Инструкция:\n{instructions}\n\n"
        f"Извлечённые факты из веба:\n{facts}\n\n"
        f"Источники:\n{source_list}\n\n"
        f"Напиши блок строго по инструкции, опираясь на факты выше."
    )
    return call_gemini(prompt, model)


def run_block_search_first(analysis: str, block_id: int, model: str) -> dict:
    """Full search-first pipeline for a single block."""
    block = BLOCKS[block_id]
    print(f"[search-first] Блок {block_id}: {block['name']}")

    # Step 1 — queries from block config
    queries = [q.format(analysis=analysis) for q in block.get("search_queries", [])]
    print(f"[search-first] Запросы: {queries}")

    # Step 2 — Serper search + Tavily extract
    sources = search_and_extract(queries, max_urls=3)
    print(f"[search-first] Источников: {len(sources)}")

    total_usage: dict = {}
    total_cost = 0.0

    # Step 3 — lite model extracts relevant facts from raw HTML
    print(f"[search-first] Извлекаем факты ({EXTRACT_MODEL})…")
    facts_raw, usage_extract = _extract_facts_from_sources(analysis, block, sources)
    cost_extract = estimate_cost(usage_extract, EXTRACT_MODEL)
    total_cost += cost_extract
    for k, v in usage_extract.items():
        total_usage[k] = total_usage.get(k, 0) + v
    print(f"[search-first] Факты: {len(facts_raw)} символов, стоимость: ${cost_extract:.6f}")

    # Step 4 — strong model writes the block from clean facts
    print(f"[search-first] Генерируем блок ({GENERATE_MODEL})…")
    block_raw, usage_gen = _generate_block_from_facts(analysis, block, facts_raw, sources, GENERATE_MODEL)
    cost_gen = estimate_cost(usage_gen, GENERATE_MODEL)
    total_cost += cost_gen
    for k, v in usage_gen.items():
        total_usage[k] = total_usage.get(k, 0) + v
    print(f"[search-first] Готово, стоимость: ${cost_gen:.6f}")

    return {
        "id":             block_id,
        "name":           block["name"],
        "section":        block["section"],
        "pass_group":     block["pass_group"],
        "format":         block["format"],
        "content":        block_raw.strip(),
        "confidence":     "high",
        "needs_verify":   False,
        "verified":       True,
        "sources":        [s["url"] for s in sources],
        "search_queries": queries,
        "search_results": [
            {"url": s["url"], "title": s["title"], "snippet": s["snippet"]}
            for s in sources
        ],
        "extracted_facts": facts_raw.strip(),
        "warnings":       [],
        "fact_cards":     [],
        "verify_queries": [q.format(analysis=analysis) for q in block.get("verify_queries", [])],
        "_usage":         total_usage,
        "_cost":          total_cost,
    }


def run(analysis: str, block_ids: list | None, model: str, out_dir: Path):
    print(f"[pipeline] Анализ: {analysis}")
    print(f"[pipeline] Блоки: {block_ids or 'все'}")
    print(f"[pipeline] Модель: {model}")

    target = set(block_ids) if block_ids else set(BLOCKS.keys())
    search_ids = target & SEARCH_FIRST_BLOCKS
    llm_ids    = target - SEARCH_FIRST_BLOCKS

    all_blocks: dict[int, dict] = {}
    total_cost = 0.0
    total_usage: dict = {}

    # ── Search-first blocks ───────────────────────────────────────────────────
    for bid in search_ids:
        if bid not in BLOCKS:
            print(f"[pipeline] Блок {bid} не найден в реестре, пропускаем")
            continue
        result = run_block_search_first(analysis, bid, model)
        total_cost += result.pop("_cost", 0)
        u = result.pop("_usage", {})
        for k, v in u.items():
            total_usage[k] = total_usage.get(k, 0) + v
        all_blocks[bid] = result

    # ── LLM-only blocks ───────────────────────────────────────────────────────
    llm_id_list = [bid for bid in llm_ids if bid in BLOCKS]
    if llm_id_list:
        print(f"[pipeline] LLM блоки: {llm_id_list}")
        prompt = build_prompt(analysis, llm_id_list)
        print(f"[pipeline] Промпт: {len(prompt)} символов")
        print("[pipeline] Вызываем Gemini…")
        raw, usage = call_gemini(prompt, model)
        cost = estimate_cost(usage, model)
        total_cost += cost
        for k, v in usage.items():
            total_usage[k] = total_usage.get(k, 0) + v
        print(f"[pipeline] Получено {len(raw)} символов, стоимость: ${cost:.6f}")
        parsed = parse_response(raw, analysis, llm_id_list)
        all_blocks.update(parsed)
        print(f"[pipeline] Распознано LLM блоков: {len(parsed)}")

    print(f"[pipeline] Итого блоков: {len(all_blocks)}, стоимость: ${total_cost:.6f}")

    slug = slugify(analysis)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.json"

    # Merge with existing result when running partial blocks
    merged_blocks = {}
    if block_ids and out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            merged_blocks = {int(k): v for k, v in existing.get("blocks", {}).items()}
            print(f"[pipeline] Мердж с существующим результатом ({len(merged_blocks)} блоков)")
        except Exception as e:
            print(f"[pipeline] Не удалось загрузить существующий результат: {e}")
    merged_blocks.update(all_blocks)

    result = {
        "analysis":     analysis,
        "slug":         slug,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "blocks":       merged_blocks,
        "stats": {
            "blocks_total":    len(merged_blocks),
            "blocks_filled":   len(merged_blocks),
            "blocks_verified": sum(1 for b in merged_blocks.values() if b.get("verified")),
            "llm_cost_usd":    round(total_cost, 6),
            "llm_tokens":      total_usage,
            "sources_used":    sum(len(b.get("sources", [])) for b in merged_blocks.values()),
        },
    }

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[pipeline] Сохранено: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Gemotest pipeline")
    parser.add_argument("--analysis", required=True, help="Название анализа")
    parser.add_argument("--blocks", default="", help="ID блоков через запятую (пусто = все)")
    parser.add_argument("--model", default="gemini-2.0-flash", help="Модель для LLM-only блоков")
    parser.add_argument("--out-dir", default=None, help="Папка для результатов")
    args = parser.parse_args()

    block_ids = None
    if args.blocks.strip():
        block_ids = [int(x.strip()) for x in args.blocks.split(",") if x.strip()]

    repo_root = Path(__file__).parent.parent.parent
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "docs" / "gemotest_pipeline" / "results"

    run(args.analysis, block_ids, args.model, out_dir)


if __name__ == "__main__":
    main()
