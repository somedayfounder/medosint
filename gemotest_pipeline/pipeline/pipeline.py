import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .llm import build_prompt, call_gemini, parse_response, estimate_cost


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


def run(analysis: str, block_ids: list | None, model: str, out_dir: Path):
    print(f"[pipeline] Анализ: {analysis}")
    print(f"[pipeline] Блоки: {block_ids or 'все'}")
    print(f"[pipeline] Модель: {model}")

    print("[pipeline] Строим промпт...")
    prompt = build_prompt(analysis, block_ids)
    print(f"[pipeline] Промпт: {len(prompt)} символов")

    print("[pipeline] Вызываем Gemini...")
    raw, usage = call_gemini(prompt, model)
    cost = estimate_cost(usage, model)
    print(f"[pipeline] Получено {len(raw)} символов, токены: {usage}, стоимость: ${cost}")

    print("[pipeline] Парсим ответ...")
    blocks = parse_response(raw, analysis, block_ids)
    print(f"[pipeline] Распознано блоков: {len(blocks)}")

    slug = slugify(analysis)
    result = {
        "analysis":   analysis,
        "slug":       slug,
        "model":      model,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "blocks":     blocks,
        "raw":        raw,
        "stats": {
            "blocks_total":    len(blocks),
            "blocks_filled":   len(blocks),
            "blocks_verified": 0,
            "llm_cost_usd":    cost,
            "llm_tokens":      usage,
            "sources_used":    0,
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
