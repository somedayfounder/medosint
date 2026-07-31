"""
Serper (Google search) + Tavily (page extraction) helpers.
"""
import os
import requests


def serper_search(query: str, num: int = 5) -> list[dict]:
    """Search via Serper API. Returns list of {title, url, snippet}."""
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise RuntimeError("SERPER_API_KEY not set")
    r = requests.post(
        "https://google.serper.dev/search",
        json={"q": query, "num": num, "gl": "ru", "hl": "ru"},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    results = []
    for item in data.get("organic", []):
        results.append({
            "title":   item.get("title", ""),
            "url":     item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


def tavily_extract(urls: list[str]) -> list[dict]:
    """Extract content from pages via Tavily API. Returns list of {url, content}."""
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set")
    r = requests.post(
        "https://api.tavily.com/extract",
        json={"urls": urls, "api_key": api_key},
        timeout=20,
    )
    if not r.ok:
        print(f"[tavily] HTTP {r.status_code}: {r.text[:200]}")
        return []
    data = r.json()
    out = []
    for item in data.get("results", []):
        out.append({
            "url":     item.get("url", ""),
            "content": item.get("raw_content", "")[:4000],
        })
    return out


def search_and_extract(queries: list[str], results_per_query: int = 10, urls_per_query: int = 3) -> list[dict]:
    """
    For each query: Serper fetches results_per_query results, take top urls_per_query unique URLs.
    Then send all collected URLs (up to len(queries)*urls_per_query) to Tavily in one batch.
    Returns list of {url, title, snippet, content}.
    """
    seen_urls: set[str] = set()
    candidates: list[dict] = []

    for q in queries:
        print(f"[search] Serper ({results_per_query} результатов): {q!r}")
        try:
            results = serper_search(q, num=results_per_query)
            added = 0
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    candidates.append(r)
                    added += 1
                if added >= urls_per_query:
                    break
            print(f"[search]   → {added} новых URL")
        except Exception as e:
            print(f"[search] Serper error: {e}")

    urls_to_extract = [c["url"] for c in candidates]
    print(f"[search] Tavily batch: {len(urls_to_extract)} URL → {urls_to_extract}")

    pages_by_url: dict[str, str] = {}
    try:
        pages = tavily_extract(urls_to_extract)
        pages_by_url = {p["url"]: p["content"] for p in pages}
        print(f"[search] Tavily извлёк: {len(pages_by_url)} страниц")
    except Exception as e:
        print(f"[search] Tavily error: {e}")

    return [
        {
            "url":     c["url"],
            "title":   c["title"],
            "snippet": c["snippet"],
            "content": pages_by_url.get(c["url"], c["snippet"]),
        }
        for c in candidates
    ]
