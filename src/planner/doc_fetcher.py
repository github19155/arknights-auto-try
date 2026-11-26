"""按需读取本地知识卡、MAA 官方协议与 PRTS，远端缓存有效期为七天。"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC_CACHE = ROOT / "data" / "doc_cache"
TERM_CACHE = ROOT / "data" / "cache_ondemand" / "terms"
USER_AGENT = "arknights-auto-try/0.1 (local automation research)"
DEFAULT_TIMEOUT = 30.0
TTL_DAYS = 7
PRTS_API = "https://prts.wiki/api.php"
_RAW_BASE = "https://raw.githubusercontent.com/MaaAssistantArknights/MaaAssistantArknights/master/docs/zh-cn/protocol/"
_HTML_BASE = "https://docs.maa.plus/zh-cn/protocol/"
MAA_TOPICS: dict[str, dict] = {
    topic: {"file": file, "url": _RAW_BASE + file,
            "fallback_url": _HTML_BASE + file.removesuffix(".md") + ".html"}
    for topic, file in {
        "copilot_schema": "copilot-schema.md",
        "task_schema": "task-schema.md",
        "callback_schema": "callback-schema.md",
        "integration": "integration.md",
        "roguelike_schema": "integrated-strategy-schema.md",
    }.items()
}
_LOCAL_CARDS = {"local_pitfalls": "执行与识别注意事项", "combat_core": "通用排轴常识"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT,
              etag: str | None = None, last_modified: str | None = None) -> dict:
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status": response.status, "body": response.read(),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "source_url": response.geturl()}
    except urllib.error.HTTPError as exc:
        if exc.code != 304:
            raise
        result = {"status": 304, "body": b"", "etag": exc.headers.get("ETag"),
                  "last_modified": exc.headers.get("Last-Modified"), "source_url": url}
        exc.close()
        return result


def http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT, etag: str | None = None) -> dict:
    return _http_get(url, timeout=timeout, etag=etag)


def _metadata(response: dict) -> dict:
    return {"source_url": response["source_url"], "etag": response["etag"],
            "last_modified": response["last_modified"], "fetched_at": _now(),
            "ttl_days": TTL_DAYS, "bytes": len(response["body"]), "status": response["status"]}


def _fresh(meta: dict) -> bool:
    fetched = datetime.fromisoformat(meta["fetched_at"])
    return datetime.now(timezone.utc) < fetched + timedelta(days=meta["ttl_days"])


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_wikitext(text: str) -> str:
    """保留可读正文、标题、链接标签与模板参数，去除 Wiki 排版标记。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/\s*>", "", text, flags=re.S | re.I)
    text = re.sub(r"\[\[(?:文件|File|Image|分类|Category):[^\]]*\]\]", "", text, flags=re.I)
    text = re.sub(r"\[\[([^\[\]]+)\]\]", lambda m: m[1].split("|")[-1], text)
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)
    # 嵌套模板由内向外展开，不能整块删除：机制说明常藏在模板参数内。
    template = re.compile(r"\{\{([^{}]*)\}\}")
    while template.search(text):
        text = template.sub(lambda m: " ".join(
            part.split("=", 1)[1] if re.match(r"^[\w-]+\s*=", part.strip()) else part
            for part in m[1].split("|")[1:]
        ), text)
    text = re.sub(r"(?m)^\s*(={1,6})\s*(.*?)\s*\1\s*$",
                  lambda m: "#" * len(m[1]) + " " + m[2], text)
    text = re.sub(r"(?m)^[ \t]*\|-.*$", "\n", text)
    text = re.sub(r"(?m)^[ \t]*\{\|.*$|^[ \t]*\|\}.*$", "", text)
    text = re.sub(r'(?m)^[ \t]*[|!]?(?:(?:rowspan|colspan|width|style|class)=[^|\n]+\|)+', "", text)
    text = re.sub(r"(?m)^[ \t]*[|!]", "", text).replace("||", " | ").replace("!!", " | ")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", text).strip()


def slice_markdown(text: str, keyword: str = "") -> str:
    """按二级标题返回相关段；正文命中时也只返回所在段而非整篇。"""
    if not keyword.strip():
        return text
    needle = keyword.casefold().strip()
    sections = re.split(r"(?m)(?=^##\s+)", text)
    titled = [part.strip() for part in sections
              if part.startswith("## ") and needle in part.splitlines()[0].casefold()]
    if titled:
        return "\n\n".join(titled)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if needle in line.casefold():
            start = max(0, index - 10)
            stop = min(len(lines), index + 26)
            return "\n".join(lines[start:stop]) + (
                f"\n[slice: keyword={keyword} lines={start + 1}-{stop} of {len(lines)}; truncated]"
            )
    return "\n".join(lines[:40]) + "\n[slice: keyword not found; showing first 40 lines]"


def _prts_url(**params: object) -> str:
    return PRTS_API + "?" + urllib.parse.urlencode(params)


def search_prts(query: str, limit: int = 5) -> list[str]:
    url = _prts_url(action="query", list="search", srsearch=query, srlimit=limit,
                    format="json", formatversion=2)
    result = json.loads(http_get(url)["body"])
    if "error" in result:
        raise LookupError(f"PRTS 搜索失败：{result['error']}")
    return [entry["title"] for entry in result["query"]["search"]]


def _parse_prts(title: str, *, etag: str | None = None,
                last_modified: str | None = None) -> dict:
    url = _prts_url(action="parse", page=title, prop="wikitext", format="json",
                    formatversion=2, redirects=1)
    response = _http_get(url, etag=etag, last_modified=last_modified)
    if response["status"] == 304:
        return response
    payload = json.loads(response["body"])
    if "error" in payload:
        error = payload["error"]
        if error["code"] == "missingtitle":
            raise LookupError(f"PRTS 页面不存在：{title}")
        raise RuntimeError(f"PRTS 解析失败：{error}")
    parsed = payload["parse"]
    return {**_metadata(response), "title": parsed["title"], "wikitext": parsed["wikitext"],
            "clean_text": clean_wikitext(parsed["wikitext"])}


def fetch_prts_page(title: str, refresh: bool = False) -> dict:
    """获取完整页面；页面缓存由关卡调用方持有，词条缓存由 fetch_prts_term 持有。"""
    return _parse_prts(title)


def _term_record(term: str, page: dict) -> dict:
    record = {key: page[key] for key in (
        "title", "source_url", "etag", "last_modified", "fetched_at", "ttl_days", "bytes", "status")}
    record.update(term=term, clean_text=page["clean_text"], truncated=False)
    if len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")) <= 3072:
        return record
    record["truncated"] = True
    # 先选命中词条名的段，再补充前言；按 UTF-8 实际落盘字节预算截断。
    parts = re.split(r"\n\s*\n|(?=^#{1,6}\s+)", page["clean_text"], flags=re.M)
    relevant = [p for p in parts if term.casefold() in p.casefold()]
    relevant.extend(p for p in parts if p not in relevant)
    content = "\n\n".join(relevant)
    record["clean_text"] = ""
    budget = 3072 - len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")) - 32
    snippet = content.encode("utf-8")[:max(0, budget)].decode("utf-8", errors="ignore")
    record["clean_text"] = snippet + "\n（正文已截断）"
    # JSON 会转义换行、引号，最终以序列化大小为准。
    while len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")) > 3072:
        snippet = snippet[:-16]
        record["clean_text"] = snippet + "\n（正文已截断）"
        if not snippet:
            raise ValueError("词条名或元数据过长，无法生成 3KB 以内的缓存")
    return record


def fetch_prts_term(term: str, refresh: bool = False) -> str:
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', lambda m: urllib.parse.quote(m[0], safe=""), term)
    path = TERM_CACHE / (filename + ".json")
    cached = _read_json(path) if path.exists() else None
    if cached and not refresh and _fresh(cached):
        return cached["clean_text"]
    request_title = term
    if cached:
        request_title = urllib.parse.parse_qs(urllib.parse.urlsplit(cached["source_url"]).query)["page"][0]
    try:
        page = _parse_prts(request_title,
                           etag=cached["etag"] if cached else None,
                           last_modified=cached["last_modified"] if cached else None)
    except LookupError:
        titles = search_prts(term)
        if not titles:
            raise LookupError(f"PRTS 页面与搜索均未命中：{term}") from None
        page = fetch_prts_page(titles[0], refresh=refresh)
    if page["status"] == 304:
        if cached is None:
            raise RuntimeError("PRTS 在没有缓存时返回 304")
        cached["fetched_at"] = _now()
        _write_json(path, cached)
        return cached["clean_text"]
    record = _term_record(term, page)
    _write_json(path, record)
    return record["clean_text"]


class _DocHTML(HTMLParser):
    """仅提取 VuePress 正文，避免把导航和脚本混入离线协议。"""
    def __init__(self) -> None:
        super().__init__()
        self.body: list[str] = []
        self.main_depth = 0
        self.skip_depth = 0
        self.pre_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self.main_depth += 1
        if not self.main_depth:
            return
        if tag in {"script", "style", "nav"}:
            self.skip_depth += 1
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.body.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "li", "tr"}:
            self.body.append("\n")
        elif tag == "br":
            self.body.append("\n")
        elif tag in {"td", "th"}:
            self.body.append(" | ")
        elif tag == "pre":
            self.pre_depth += 1
            self.body.append("\n\n```\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.main_depth:
            return
        if tag in {"script", "style", "nav"}:
            self.skip_depth -= 1
        elif not self.skip_depth:
            if tag == "pre":
                self.pre_depth -= 1
                self.body.append("\n```\n\n")
            elif tag in {"p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
                self.body.append("\n")
        if tag == "main":
            self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.main_depth and not self.skip_depth:
            self.body.append(data if self.pre_depth else re.sub(r"\s+", " ", data))


def fetch_doc(topic: str, refresh: bool = False) -> Path:
    spec = MAA_TOPICS[topic]
    path = DOC_CACHE / f"{topic}.md"
    meta_path = DOC_CACHE / f"{topic}.meta.json"
    meta = _read_json(meta_path) if path.exists() and meta_path.exists() else None
    if meta and not refresh and _fresh(meta):
        return path
    # 验证器仅对其原始资源有效，不能把 HTML 的 ETag 发给 raw。
    primary_meta = meta if meta and meta["source_url"] == spec["url"] else None
    try:
        response = _http_get(spec["url"], etag=primary_meta["etag"] if primary_meta else None,
                             last_modified=primary_meta["last_modified"] if primary_meta else None)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        fallback_meta = meta if meta and meta["source_url"] == spec["fallback_url"] else None
        response = _http_get(spec["fallback_url"], etag=fallback_meta["etag"] if fallback_meta else None,
                             last_modified=fallback_meta["last_modified"] if fallback_meta else None)
    if response["status"] == 304:
        if meta is None:
            raise RuntimeError("MAA 文档在没有缓存时返回 304")
        meta["fetched_at"] = _now()
        _write_json(meta_path, meta)
        return path
    text = response["body"].decode("utf-8-sig")
    if response["source_url"].split("?", 1)[0].endswith(".html"):
        parser = _DocHTML()
        parser.feed(text)
        text = re.sub(r"\n{3,}", "\n\n", "".join(parser.body)).strip()
    if not text.strip():
        raise ValueError(f"MAA 文档正文为空：{response['source_url']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _write_json(meta_path, _metadata(response))
    return path


def get_doc_slice(topic_or_term: str, section_keyword: str = "", refresh: bool = False) -> str:
    if topic_or_term in _LOCAL_CARDS:
        text = (ROOT / "docs" / "kb" / f"{topic_or_term}.md").read_text(encoding="utf-8")
    elif topic_or_term in MAA_TOPICS:
        text = fetch_doc(topic_or_term, refresh=refresh).read_text(encoding="utf-8")
    else:
        text = fetch_prts_term(topic_or_term, refresh=refresh)
    return slice_markdown(text, section_keyword)


def lookup(query: str, section: str = "", refresh: bool = False) -> str:
    query = query.strip()
    if not query:
        lines = ["# 按需知识主题", "", "## 本地知识卡"]
        lines.extend(f"- {topic}：{label}" for topic, label in _LOCAL_CARDS.items())
        lines.append("## MAA 官方协议（七天缓存）")
        lines.extend(f"- {topic}" for topic in MAA_TOPICS)
        lines.extend(["## PRTS 百科", "- 其他查询按词条名解析；缺页时搜索首个结果。",
                      '示例：lookup("copilot_schema", section="actions")',
                      '示例：lookup("灼燃损伤")；lookup("local_pitfalls")'])
        return "\n".join(lines)
    result = get_doc_slice(query, section, refresh=refresh)
    # 空 section 时禁止整篇倾倒：超长文档只给前 40 行 + 章节索引。
    lines = result.splitlines()
    if not section and len(lines) > 120:
        heads = [line for line in lines if line.startswith("## ")]
        outline = "\n".join(f"- {h[3:]}" for h in heads[:30])
        return (f"{chr(10).join(lines[:40])}\n\n## 章节索引\n{outline}\n"
                f"[doc: 已截断，全文 {len(lines)} 行；用 section= 精确取段]")
    return result
