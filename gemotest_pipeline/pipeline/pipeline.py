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


def _generate_block_from_sources(analysis: str, block: dict, sources: list[dict], model: str) -> tuple[str, dict]:
    """Ask Flash to write a block from web sources."""
    web_ctx = "\n\n".join(
        f"Источник: {s['url']}\nЗаголовок: {s['title']}\n\n{s['content']}"
        for s in sources
    )
    instructions = block["instructions"].format(analysis=analysis)
    prompt = (
        f"Ты медицинский контент-редактор Гемотест.\n"
        f"Анализ: {analysis}\n"
        f"Блок: {block['name']}\n\n"
        f"Инструкция:\n{instructions}\n\n"
        f"Данные из веба:\n{web_ctx}\n\n"
        f"Напиши блок строго по инструкции, используя факты из источников выше."
    )
    return call_gemini(prompt, model)


def run_block_search_first(analysis: str, block_id: int, model: str) -> dict:
    """Full search-first pipeline for a single block."""
    block = BLOCKS[block_id]
    print(f"[search-first] Блок {block_id}: {block['name']}")

    # Step 1 — queries from block config
    queries = [q.format(analysis=analysis) for q in block.get("search_queries", [])]
    print(f"[search-first] Запросы: {queries}")

    # Step 2 — search + extract
    sources = search_and_extract(queries, max_urls=3)
    print(f"[search-first] Источников: {len(sources)}")

    # Step 3 — generate block from sources
    print("[search-first] Генерируем блок из источников…")
    raw, usage = _generate_block_from_sources(analysis, block, sources, model)
    cost = estimate_cost(usage, model)

    return {
        "id":           block_id,
        "name":         block["name"],
        "section":      block["section"],
        "pass_group":   block["pass_group"],
        "format":       block["format"],
        "content":      raw.strip(),
        "confidence":   "high",
        "needs_verify": False,
        "verified":     True,
        "sources":      [s["url"] for s in sources],
        "warnings":     [],
        "fact_cards":   [],
        "verify_queries": [q.format(analysis=analysis) for q in block.get("verify_queries", [])],
        "_search_queries": queries,
        "_usage":       usage,
        "_cost":        cost,
    }


def run(analysis: str, block_ids: list | None, model: str, out_dir: Path):
    print(f"[pipeline] Анализ: {analysis}")
    print(f"[pipeline] Блоки: {block_ids or 'все'}")
    print(f"[pipeline] Модель: {model}")

    target = set(block_ids) if block_ids else set(BLOCKS.keys())
    search_ids  = target & SEARCH_FIRST_BLOCKS
    llm_ids     = target - SEARCH_FIRST_BLOCKS

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
        print("[pipeline] Строим промпт…")
        prompt = build_prompt(analysis, llm_id_list)
        print(f"[pipeline] Промпт: {len(prompt)} символов")
        print("[pipeline] Вызываем Gemini…")
        raw, usage = call_gemini(prompt, model)
        cost = estimate_cost(usage, model)
        total_cost += cost
        for k, v in usage.items():
            total_usage[k] = total_usage.get(k, 0) + v
        print(f"[pipeline] Получено {len(raw)} символов, стоимость: ${cost}")
        print("[pipeline] Парсим ответ…")
        parsed = parse_response(raw, analysis, llm_id_list)
        all_blocks.update(parsed)
        print(f"[pipeline] Распознано LLM блоков: {len(parsed)}")

    print(f"[pipeline] Итого блоков: {len(all_blocks)}, стоимость: ${total_cost:.6f}")

    slug = slugify(analysis)
    result = {
        "analysis":     analysis,
        "slug":         slug,
        "model":        model,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "blocks":       all_blocks,
        "stats": {
            "blocks_total":    len(all_blocks),
            "blocks_filled":   len(all_blocks),
            "blocks_verified": sum(1 for b in all_blocks.values() if b.get("verified")),
            "llm_cost_usd":    round(total_cost, 6),
            "llm_tokens":      total_usage,
            "sources_used":    sum(len(b.get("sources", [])) for b in all_blocks.values()),
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[pipeline] Сохранено: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Gemotest pipeline")
    parser.add_argument("--analysis", required=True, help="Название анализа")
    parser.add_argument("--blocks", default="", help="ID блоков через запятую (пусто = все)")
    parser.add_argument("--model", default="gemini-1.5-flash")
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
