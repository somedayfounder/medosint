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


def search_and_extract(queries: list[str], max_urls: int = 3) -> list[dict]:
    """
    Run Serper for each query, deduplicate URLs, extract top pages via Tavily.
    Returns list of {url, title, snippet, content}.
    """
    seen_urls: set[str] = set()
    candidates: list[dict] = []

    for q in queries:
        print(f"[search] Serper: {q!r}")
        try:
            results = serper_search(q, num=5)
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    candidates.append(r)
        except Exception as e:
            print(f"[search] Serper error: {e}")

    top_urls = [c["url"] for c in candidates[:max_urls]]
    print(f"[search] Tavily extract: {top_urls}")

    extracted = []
    try:
        pages = tavily_extract(top_urls)
        pages_by_url = {p["url"]: p["content"] for p in pages}
    except Exception as e:
        print(f"[search] Tavily error: {e}")
        pages_by_url = {}

    for c in candidates[:max_urls]:
        extracted.append({
            "url":     c["url"],
            "title":   c["title"],
            "snippet": c["snippet"],
            "content": pages_by_url.get(c["url"], c["snippet"]),
        })

    return extracted
