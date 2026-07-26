#!/usr/bin/env python3
"""ingest.py — The Economist EPUB -> Supabase 一站式入库（v3：TOC 定位 + 位置顺序提取）

用法:
    python ingest.py <epub路径>               # 提取并直接上传 Supabase（默认，不落本地文件）
    python ingest.py <epub路径> --dump        # 同时把 JSON+图片落一份到 output/ 备查
    python ingest.py <epub路径> --no-upload   # 只提取到 output/，不上传

============================================================
架构（PLAN-v3 定稿）：TOC 定位 + 位置顺序提取，format-agnostic
============================================================
不为三种 EPUB 来源写三条路径，而是用两条真共性覆盖所有经济学人 EPUB：
  共性1: EPUB 必有 TOC，ebooklib 读 book.toc 统一输出"版块 → 文章 href"
  共性2: 文章第一页固定 6 位置 layout（栏目标签→主标题→副标题→日期→题图→正文）
  共性3: 正文以 ■ 结尾（U+25A0）
  共性4: ■ 之后到下一篇文章之间是广告/水印/分隔，不进数据库

流程（PLAN-v3 §6）:
  1. ebooklib 读 TOC -> [(版块名, [(文章标题, 文章href), ...]), ...]
  2. 每个 TOC 条目 = 一篇文章起点
  3. 文章范围 = 当前 href -> 下一 href（跨文件自动拼接）
  4. 范围内按 6 位置顺序提取字段（不靠颜色/class）:
     位置1 短文本      -> column_label
     位置2 长文本      -> title（h1 或 p 都接受）
     位置3 中等文本    -> subtitle（可选，无则 NULL）
     位置4 日期正则    -> published_at + location
     位置5 含img块     -> lead 题图
     位置6+ 段落       -> body_blocks + body_text
  5. ■ 截断（保证广告不进数据库）: 找 ■ -> 在 ■ 处截断正文（保留含■段，剥■字符；之后全丢）
  6. 基本清洗 + 入库
  7. 跳过零正文段切片（经济数据页）

安全:
  - 校验有错误(errors>0)时不执行上传
  - 幂等: 同一 pub_date 的刊已存在则先整刊删除(外键级联)再重灌
  - 密钥从 .env 读取 (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)

原则: 字段用位置+文本特征识别不靠颜色/class；纯代码提取不调 AI API；
      铁律#2 调整为"禁原始 HTML 字符串，允许结构化 runs"（body_blocks 是干净 JSON，非 HTML 源码）。

显式假设清单（PLAN-v3 §11，违反时大声报错，不静默产出垃圾）:
  H1. EPUB 合法（ebooklib 能读 + 有 toc）—— 否则 abort
  H2. TOC 树至少 1 个版块含文章 href —— 否则 abort + 提示"TOC 无文章链接"
  H3. 文章 href 指向的文件存在于 zip —— 否则 skip + log
  H4. 文章第一页能按位置提取到标题（位置2）—— 否则 skip + log
  H5. 日期行匹配既有正则 —— 未匹配则 published_at=NULL + warn（不阻塞）
  H6. TOC 缺失则 warn，section="Unknown"，不阻塞
"""

import argparse
import json
import mimetypes
import os
import re
import sys
import unicodedata
import warnings
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import ebooklib
from ebooklib import epub
from ebooklib.epub import Link, Section
from bs4 import BeautifulSoup, NavigableString, Tag

warnings.filterwarnings("ignore")  # ebooklib/lxml 的提示静音

BUCKET = "article-images"

# ---------------------------------------------------------------- 清洗规则（PLAN-v3 §8）

# 段落黑名单（整段丢弃）
DROP_PARAGRAPH_PATTERNS = [
    re.compile(r"t\.me/", re.I),
    re.compile(r"^Dig deeper\b", re.I),
    re.compile(r"You can see previous ones here", re.I),
]

# 订阅广告段首正则（仅段首命中，防 "sign up to the treaty" 误杀）
AD_FIRST_RE = re.compile(
    r"^(Sign up to|Subscribe to|To enjoy|Read more of|For more from|"
    r"Want more|Keep up with|For the best of|To get more)",
    re.I,
)

# 来源水印
WATERMARK_RE = re.compile(r"This article was downloaded by .+? from", re.I)

# 日期行三格式（PLAN-v3 §7）
# 格式1 英文: "Jul 09, 2026 05:22 AM" 或 "Jul 09, 2026 05:22 AM | Singapore"
DATE_EN_RE = re.compile(
    r"^([A-Za-z]{3,9} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)\s*(?:\|\s*(.+))?$"
)
# 格式2 中文: "6月 18, 2026 03:17 上午" 或 "... | BEIJING"
DATE_ZH_RE = re.compile(
    r"^(\d{1,2}月 \d{1,2}, \d{4} \d{1,2}:\d{2}\s*(?:上午|下午))\s*(?:\|\s*(.+))?$"
)
# 格式3 序数英文: "June 11th 2026"（无时间，published_at 留日期即可）
DATE_ORD_RE = re.compile(
    r"^([A-Za-z]{3,9} \d{1,2}(?:st|nd|rd|th) \d{4})$"
)

WORD_RE = re.compile(r"[A-Za-z0-9'’\-]+")

# ■ 结束符（U+25A0）
ENDMARK = "■"


def normalize_text(raw: str) -> str:
    """单段清洗: 交叉引用分隔符 · -> 空格; 空白归一; 剥尾部 ■; Unicode NFC。"""
    text = raw.replace("·", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*■\s*$", "", text).strip()
    return unicodedata.normalize("NFC", text)


def keep_paragraph(text: str) -> bool:
    """段是否保留: 空丢; 黑名单丢; 段首订阅广告丢; 水印丢。"""
    if not text:
        return False
    if any(p.search(text) for p in DROP_PARAGRAPH_PATTERNS):
        return False
    if AD_FIRST_RE.match(text):
        return False
    if WATERMARK_RE.search(text):
        return False
    return True


def parse_date_line(text: str):
    """匹配日期行 -> (published_at_iso, location) 或 (None, None)。
    依次尝试三格式。源站未给时区按 UTC 记。
    """
    if not text:
        return None, None
    # 格式1 英文
    m = DATE_EN_RE.match(text)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%b %d, %Y %I:%M %p")
            loc = m.group(2)
            return dt.isoformat() + "+00:00", (loc.strip() if loc else None)
        except ValueError:
            pass
    # 格式2 中文
    m = DATE_ZH_RE.match(text)
    if m:
        try:
            raw = m.group(1).replace("上午", "AM").replace("下午", "PM")
            dt = datetime.strptime(raw, "%m月 %d, %Y %I:%M %p")
            loc = m.group(2)
            return dt.isoformat() + "+00:00", (loc.strip() if loc else None)
        except ValueError:
            pass
    # 格式3 序数英文
    m = DATE_ORD_RE.match(text)
    if m:
        try:
            s = m.group(1)
            # 去 ordinal 后缀
            s2 = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", s)
            dt = datetime.strptime(s2, "%B %d %Y")
            return dt.date().isoformat(), None
        except ValueError:
            pass
    return None, None


# ---------------------------------------------------------------- 行内 runs 提取（PLAN-v3 §10，复用旧版）

def extract_runs(el) -> list:
    """从元素提取行内 runs -> [{t:text|b|i|sc|a, x:..., href?}, ...]
    同类型相邻 run 合并。
    """
    runs = []

    def push(t, x, href=None):
        if x is None:
            return
        x = unicodedata.normalize("NFC", str(x))
        if not x:
            return
        if runs and runs[-1]["t"] == t and runs[-1].get("href") == href:
            runs[-1]["x"] += x
        else:
            r = {"t": t, "x": x}
            if href:
                r["href"] = href
            runs.append(r)

    def walk(node):
        if isinstance(node, NavigableString):
            push("text", str(node))
            return
        if not isinstance(node, Tag):
            return
        name = (node.name or "").lower()
        if name in ("b", "strong"):
            inner = node.get_text("")
            if inner:
                push("b", inner)
            return
        if name in ("i", "em"):
            inner = node.get_text("")
            if inner:
                push("i", inner)
            return
        if name == "a":
            href = node.get("href")
            inner = node.get_text("")
            if inner:
                push("a", inner, href)
            return
        if name == "span":
            cls = " ".join(node.get("class") or [])
            style = node.get("style", "")
            if ("sc" in cls.lower() or "small-caps" in style.lower()
                    or "smallcaps" in cls.lower()):
                inner = node.get_text("")
                if inner:
                    push("sc", inner)
                return
        for child in node.children:
            walk(child)

    walk(el)
    # 合并相邻同类
    merged = []
    for r in runs:
        if merged and merged[-1]["t"] == r["t"] and merged[-1].get("href") == r.get("href"):
            merged[-1]["x"] += r["x"]
        else:
            merged.append(r)
    return merged


# ---------------------------------------------------------------- ebooklib TOC 解析（v3 核心）

def parse_toc(book):
    """ebooklib book.toc -> [(section_name, [(article_title, article_href), ...]), ...]

    ebooklib toc 可能形态:
      1. [Link, Link, ...]                    扁平（无版块）
      2. [(Section, [Link,...]), ...]         嵌套 tuple
      3. [Section, Link, Link, ...]           混合
    策略: 递归遍历；遇有子节点的节点 -> 新版块；遇叶子 Link -> 文章归当前版块。
    """
    sections = []  # [(sec_name, [(title, href), ...]), ...]
    current = {"name": None, "arts": []}

    def flush():
        if current["name"] is not None or current["arts"]:
            sections.append((current["name"], list(current["arts"])))
        current["name"] = None
        current["arts"] = []

    def node_title(node):
        return (getattr(node, "title", None) or "").strip()

    def node_href(node):
        return (getattr(node, "href", None) or "").strip()

    def walk(items):
        for item in items:
            # 嵌套 (node, children)
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (list, tuple)):
                node, children = item
                if children:
                    flush()
                    current["name"] = node_title(node) or "(unnamed section)"
                    walk(children)
                else:
                    t, h = node_title(node), node_href(node)
                    if t and h:
                        current["arts"].append((t, h))
            elif isinstance(item, Section):
                # Section 通常是无 href 的分组节点 -> 当版块
                flush()
                current["name"] = node_title(item) or "(unnamed section)"
            elif isinstance(item, Link):
                t, h = node_title(item), node_href(item)
                if t and h:
                    current["arts"].append((t, h))
            elif isinstance(item, list):
                walk(item)
            elif isinstance(item, tuple) and len(item) == 2:
                # (title, href) 简单对
                t, h = item
                if t and h:
                    current["arts"].append((str(t), str(h)))

    walk(book.toc)
    flush()
    return sections


# ---------------------------------------------------------------- 按文件路径解析 + spine 顺序

def href_to_path(href: str):
    """toc href -> (zip内文件路径, anchor)
    去掉 #anchor；规范化路径（去 ./）。
    """
    if not href:
        return None, None
    # 拆 anchor
    anchor = None
    if "#" in href:
        href, anchor = href.split("#", 1)
    # 去 query
    if "?" in href:
        href = href.split("?", 1)[0]
    # 规范化
    href = href.replace("./", "")
    return href or None, anchor


def get_ordered_docs(book):
    """按 spine 顺序返回内容文档列表 -> [item, ...]

    用文件扩展名(.html/.xhtml/.htm)过滤，**不依赖 item type**——
    部分非标准 EPUB（如 920.im 来源的 C 版式）把内容文件 media-type 标为
    'text/html' 而非 EPUB 规范要求的 'application/xhtml+xml'，ebooklib 会
    判为 ITEM_UNKNOWN(type=0)，get_items_of_type(ITEM_DOCUMENT) 拿不到。
    扩展名过滤对三版式（Calibre 英文/中文、920.im、经济学人原生）都通用。
    spine 顺序 = 阅读顺序；用于跨文件拼接文章范围。
    """
    spine_ids = [sid for sid, _linear in book.spine]
    docs = []
    for item in book.get_items():
        if not item.get_name().endswith((".html", ".xhtml", ".htm")):
            continue
        try:
            idx = spine_ids.index(item.id)
        except ValueError:
            idx = 1 << 30  # 不在 spine 的排最后
        docs.append((idx, item))
    docs.sort(key=lambda x: x[0])
    return [d for _, d in docs]


def soup_of_item(item) -> BeautifulSoup:
    raw = item.get_content().decode("utf-8", "ignore")
    return BeautifulSoup(raw, "lxml")


def clean_body(soup) -> Tag:
    """取 body，去 navbar，返回。无 body 返回 None。"""
    body = soup.find("body")
    if body is None:
        return None
    for nav in body.find_all(attrs={"class": re.compile(r"navbar")}):
        nav.decompose()
    return body


def extract_content_elements(body) -> list:
    """从 body 提取内容元素序列 -> [Tag, ...]

    经济学人文章正文常包在一个 wrapper div 里（A 版式 div.calibre-nuked-tag-article），
    body 顶层只有 navbar + 单一 wrapper div。直接取 body.children 会只拿到 1 个大 div，
    无法按 6 位置 layout 拆分。本函数下钻：若顶层只剩 1 个 div 且其含 >=2 个块级子元素，
    则用该 div 的 children 作为内容序列。
    """
    tags = [c for c in body.children if isinstance(c, Tag)]
    # 下钻单一 wrapper div（最多 3 层防无限循环）
    for _ in range(3):
        if len(tags) == 1 and tags[0].name == "div":
            inner = [c for c in tags[0].children if isinstance(c, Tag)]
            if len(inner) >= 2:
                tags = inner
                continue
        break
    return tags


def element_text(el) -> str:
    """拼接元素全部文本节点（无分隔）——首字下沉自然合并。"""
    return el.get_text("")


# ---------------------------------------------------------------- 文章范围元素收集（跨文件）

def collect_article_elements(ordered_docs, start_path, start_anchor, next_path, next_anchor):
    """收集 [start_path 文件, next_path 文件) 之间所有顶层块元素 -> [(path, tag), ...]

    - start_path == next_path（同文件多篇）: 同文件内，用 anchor 切；无 anchor 则整文件（兜底）
    - start_path != next_path: start_path 全 body 顶层元素 + 中间文件全 body 顶层元素
      （next_path 不含，因为它是下一篇起点）
    - next_path is None（最后一篇）: start_path 到末尾全部
    """
    # 找 start / next 在 ordered_docs 中的索引
    name_to_idx = {d.get_name(): i for i, d in enumerate(ordered_docs)}
    # start_path 可能是 toc 给的相对路径，未必等于 item.get_name()；做一次模糊匹配
    start_idx = _resolve_idx(name_to_idx, start_path)
    if start_idx is None:
        return [], f"start_path 未找到: {start_path}"

    if next_path:
        next_idx = _resolve_idx(name_to_idx, next_path)
        if next_idx is None:
            next_idx = len(ordered_docs)
    else:
        next_idx = len(ordered_docs)

    elements = []

    if start_idx == next_idx:
        # 同文件多篇：取该文件，用 anchor 切（若有）
        doc = ordered_docs[start_idx]
        body = clean_body(soup_of_item(doc))
        if body is None:
            return [], "start 文件无 body"
        if start_anchor:
            # 尝试定位 anchor 节点，取其后兄弟
            anchor_el = body.find(id=start_anchor)
            if anchor_el:
                # 上溯到块级
                block = _blockify(anchor_el)
                if block:
                    for sib in [block] + block.find_next_siblings():
                        if isinstance(sib, Tag):
                            elements.append((doc.get_name(), sib))
                    return elements, None
        # 无 anchor 或定位失败 -> 整文件（多篇会混，靠报告暴露）
        for child in extract_content_elements(body):
            elements.append((doc.get_name(), child))
        return elements, (None if start_anchor else "同文件多篇无anchor，整文件兜底")

    # 不同文件：start 文件全 + 中间文件全
    for fi in range(start_idx, next_idx):
        doc = ordered_docs[fi]
        body = clean_body(soup_of_item(doc))
        if body is None:
            continue
        for child in extract_content_elements(body):
            elements.append((doc.get_name(), child))
    return elements, None


INLINE_TAGS = {"span", "a", "b", "strong", "i", "em", "small", "sub", "sup", "code", "u"}


def _blockify(el):
    while el is not None and el.name in INLINE_TAGS:
        el = el.parent
    return el


def _resolve_idx(name_to_idx, path):
    """toc href 路径 -> ordered_docs 索引。精确匹配失败时做后缀/包含匹配。"""
    if not path:
        return None
    if path in name_to_idx:
        return name_to_idx[path]
    # 后缀匹配（toc 可能给相对路径，zip 内是绝对路径）
    for name, idx in name_to_idx.items():
        if name.endswith(path) or path.endswith(name):
            return idx
    return None


# ---------------------------------------------------------------- ■ 截断

def find_endmark_position(elements):
    """在区间元素里找 ■ -> 截断索引（含该元素，该元素内 ■ 由 normalize_text 剥除）。
    找不到返回 len(elements)。
    """
    for i, (path, el) in enumerate(elements):
        if ENDMARK in element_text(el):
            return i + 1
    return len(elements)


# ---------------------------------------------------------------- 6 位置 layout 提取（v3 核心）

def is_date_line(text):
    return bool(text and (DATE_EN_RE.match(text) or DATE_ZH_RE.match(text) or DATE_ORD_RE.match(text)))


def parse_article(elements, toc_title):
    """按 6 位置 layout 提取单篇 -> dict；无法解析返回 {'skip': 原因}

    elements = [(path, tag), ...] 已去 navbar、已跨文件拼接、未截断 ■
    toc_title = TOC 给的文章标题（校验/兜底用）
    """
    if not elements:
        return {"skip": "empty slice"}

    # ■ 截断
    cut = find_endmark_position(elements)
    elements = elements[:cut]

    # 收集"实质元素"：去空、去纯装饰（br/hr/空 div）
    substantive = []  # [(path, el, text, has_img)]
    for path, el in elements:
        text = normalize_text(element_text(el))
        has_img = bool(el.find("img"))
        if text or has_img:
            substantive.append((path, el, text, has_img))

    if not substantive:
        return {"skip": "no substantive elements"}

    # --- 找日期行（位置4，强信号）---
    date_idx = None
    published_at = None
    location = None
    for i, (path, el, text, has_img) in enumerate(substantive):
        if text:
            pa, loc = parse_date_line(text)
            if pa:
                published_at = pa
                location = loc
                date_idx = i
                break

    # --- 找题图（位置5，含 img 且在正文前）---
    lead_idx = None
    lead_img_el = None
    for i, (path, el, text, has_img) in enumerate(substantive):
        if has_img:
            lead_idx = i
            lead_img_el = el
            break

    # --- 头部区间上界 = 日期行 或 题图（取较早出现的）---
    # 头部 = 位置1-3（栏目标签/主标题/副标题），在日期行之前
    if date_idx is not None and lead_idx is not None:
        head_end = min(date_idx, lead_idx)
    elif date_idx is not None:
        head_end = date_idx
    elif lead_idx is not None:
        head_end = lead_idx
    else:
        head_end = 0  # 无日期无题图 -> 头部候选用前 3 个 text 元素

    # 头部 text 候选（位置1-3）
    if head_end > 0:
        head_text = [(p, e, t) for p, e, t, hi in substantive[:head_end] if t and not hi]
    else:
        head_text = [(p, e, t) for p, e, t, hi in substantive[:5] if t and not hi]

    # --- 位置1 column_label：头部第一个短文本（≤80 字符，非日期）---
    # 阈值 80 容纳 "版块名 | kicker" 形式（如 "Finance & economics | A game of two halves" 43字符）
    column_label = None
    if head_text:
        p0, e0, t0 = head_text[0]
        if len(t0) <= 80 and not is_date_line(t0):
            column_label = t0

    # --- 位置2 title：首个 h1；无则头部第一个长文本(>40)或非 column_label 的文本；兜底 toc 标题 ---
    title = None
    title_el = None
    # 优先 h1
    for path, el, text, has_img in substantive:
        if el.name == "h1":
            t = normalize_text(element_text(el))
            if t:
                title = t
                title_el = el
                break
    # 无 h1：在头部候选里找
    if not title:
        for p, e, t in head_text:
            if is_date_line(t):
                continue
            if t == column_label:
                continue
            # 长文本(>40)或带标题特征(h2/h3/h4 或 p 但短) -> title
            title = t
            title_el = e
            break
    # 仍无 title -> toc 兜底
    if not title and toc_title:
        title = normalize_text(toc_title)
    if not title:
        return {"skip": "no title", "column_label": column_label}

    # --- 位置3 subtitle：title 之后、日期行之前，第一个非空 text（非日期、非 title）---
    subtitle = None
    if title_el is not None:
        # 在 substantive 中找 title_el 的位置
        title_sub_idx = None
        for i, (p, e, t, hi) in enumerate(substantive):
            if e is title_el:
                title_sub_idx = i
                break
        if title_sub_idx is not None:
            search_end = date_idx if date_idx is not None else (lead_idx if lead_idx is not None else title_sub_idx + 3)
            for j in range(title_sub_idx + 1, min(search_end, len(substantive)) if search_end > title_sub_idx else len(substantive)):
                p, e, t, hi = substantive[j]
                if hi:
                    continue
                if not t or is_date_line(t):
                    continue
                if t == column_label or t == title:
                    continue
                subtitle = t
                break

    # --- 正文起点 = 日期行之后（有日期）/ 题图之后（无日期有题图）/ 第一个长段落（兜底）---
    body_start_sub = None
    if date_idx is not None:
        # 日期行之后第一个"长段落"(p 且文本>30) 为正文起点
        for j in range(date_idx + 1, len(substantive)):
            p, e, t, hi = substantive[j]
            if hi:
                continue
            if e.name == "p" and len(t) > 30 and keep_paragraph(t):
                body_start_sub = j
                break
    if body_start_sub is None and lead_idx is not None:
        # 题图之后
        for j in range(lead_idx + 1, len(substantive)):
            p, e, t, hi = substantive[j]
            if hi:
                continue
            if e.name == "p" and len(t) > 30 and keep_paragraph(t):
                body_start_sub = j
                break
    if body_start_sub is None:
        # 兜底：第一个 p 且长文本（跳过 title_el 本身）
        for j, (p, e, t, hi) in enumerate(substantive):
            if e is title_el:
                continue
            if hi:
                continue
            if e.name == "p" and len(t) > 30 and keep_paragraph(t) and not is_date_line(t):
                body_start_sub = j
                break

    # --- 图片收集（递归 find_all img，按 id 去重）---
    images_raw = []
    seen_img_ids = set()
    # substantive 索引 -> 用于 role 判定（lead 在 body_start 之前，figure 在之后）
    sub_to_orig = {}  # substantive idx -> elements 原始 idx（用于 block_idx）
    # 重新映射：我们直接用 substantive 索引
    for i, (path, el, text, has_img) in enumerate(substantive):
        for img in el.find_all("img"):
            if id(img) in seen_img_ids:
                continue
            seen_img_ids.add(id(img))
            src = img.get("src")
            if not src or src.startswith(("http://", "https://", "data:")):
                continue
            # 图注: img.title > alt > 图下明确图注元素（class 含 caption/figcaption/credit/photo）
            # 铁律#5：绝不串借正文——只在下一兄弟明确是图注时才取，否则 NULL
            caption = (img.get("title") or "").strip() or None
            if caption is None:
                caption = (img.get("alt") or "").strip() or None
            if caption is None:
                wrapper = img.parent
                nxt = wrapper.find_next_sibling() if wrapper else None
                if nxt is not None and nxt.name in ("div", "p", "figcaption"):
                    cls = " ".join(nxt.get("class") or []).lower()
                    if (nxt.name == "figcaption" or "caption" in cls
                            or "figcaption" in cls or "credit" in cls or "photo" in cls):
                        ct = normalize_text(element_text(nxt))
                        if ct and len(ct) < 200:  # 图注通常较短，防误判长正文
                            caption = ct
            role = "lead" if (body_start_sub is None or i < body_start_sub) else "figure"
            zip_path = os.path.normpath(str((Path(path).parent / src).as_posix()))
            images_raw.append({
                "src": src,
                "zip_path": zip_path,
                "caption": caption,
                "role": role,
                "sub_idx": i,
                "_assigned": False,
            })

    # --- 正文 blocks 生成 ---
    body_blocks = []
    headings = []
    paragraphs_text = []
    img_sort_counter = 0
    images_indexed = []
    body_seen = 0

    # 先编号 lead 图（题图，在正文前）
    for im in images_raw:
        if im["role"] == "lead" and not im["_assigned"]:
            im["sort_order"] = img_sort_counter
            im["_assigned"] = True
            img_sort_counter += 1
            images_indexed.append(im)

    if body_start_sub is not None:
        for j in range(body_start_sub, len(substantive)):
            path, el, text, has_img = substantive[j]
            # 小标题
            if el.name in ("h2", "h3", "h4"):
                t = normalize_text(element_text(el))
                if t:
                    headings.append({"text": t, "sort_order": len(headings)})
                    body_blocks.append({"type": "h", "runs": extract_runs(el)})
                continue
            # 段落
            if el.name == "p":
                if has_img:
                    # 含图 p：当作内文图块
                    for im in images_raw:
                        if im["sub_idx"] == j and im["role"] == "figure" and not im["_assigned"]:
                            im["sort_order"] = img_sort_counter
                            im["_assigned"] = True
                            img_sort_counter += 1
                            images_indexed.append(im)
                            body_blocks.append({"type": "img", "ref": im["sort_order"]})
                    continue
                t = normalize_text(element_text(el))
                if not keep_paragraph(t):
                    continue
                # 首字下沉: p 首子节点 <b>/<strong> 且单字符
                dropcap = False
                first_child = el.find(True, recursive=False)
                if first_child and first_child.name in ("b", "strong"):
                    if len(first_child.get_text("")) == 1 and len(el.get_text("")) > 1:
                        dropcap = True
                runs = extract_runs(el)
                block = {"type": "p", "runs": runs}
                if dropcap:
                    block["dropcap"] = True
                body_blocks.append(block)
                paragraphs_text.append(t)
                body_seen += 1
                continue
            # div 含图 -> 内文图块
            if el.name == "div" and has_img:
                for im in images_raw:
                    if im["sub_idx"] == j and im["role"] == "figure" and not im["_assigned"]:
                        im["sort_order"] = img_sort_counter
                        im["_assigned"] = True
                        img_sort_counter += 1
                        images_indexed.append(im)
                        body_blocks.append({"type": "img", "ref": im["sort_order"]})
                continue
            # 裸 img -> 内文图块
            if el.name == "img":
                for im in images_raw:
                    if im["sub_idx"] == j and im["role"] == "figure" and not im["_assigned"]:
                        im["sort_order"] = img_sort_counter
                        im["_assigned"] = True
                        img_sort_counter += 1
                        images_indexed.append(im)
                        body_blocks.append({"type": "img", "ref": im["sort_order"]})
                continue

    if body_seen == 0:
        return {"skip": "no body paragraphs", "title": title, "column_label": column_label}

    body_text = "\n\n".join(paragraphs_text)
    # 经济数据页统一 skip（6 本行为一致）：标题匹配为主，word_count<10 兜底
    # 经济数据页是表格无实质正文，部分版式有少量表格标题段（body_seen>0）但内容为空
    wc = len(WORD_RE.findall(body_text))
    if "economic data" in title.lower() or wc < 10:
        return {"skip": f"economic data page (wc={wc})", "title": title, "column_label": column_label}
    content_type = "cartoon" if title.lower().startswith("cartoon") else "article"

    return {
        "column_label": column_label,
        "title": title,
        "subtitle": subtitle,
        "teaser": None,
        "published_at": published_at,
        "location": location,
        "body_blocks": body_blocks,
        "body_text": body_text,
        "word_count": wc,
        "content_type": content_type,
        "headings": headings,
        "images": images_indexed,
    }


# ---------------------------------------------------------------- 提取主流程

def extract(book, epub_path: Path):
    """统一提取入口 -> (issue, sections, articles, headings, images, report, errors)
    book 由调用方读好传入（upload/dump_local 也要用同一对象）。
    """
    epub_name = epub_path.name
    # H1: 有 toc（book 已由 main 读取，此处只校验 toc）
    toc_raw = book.toc
    if not toc_raw:
        return None, [], [], [], [], [f"H1 违反: book.toc 为空"], ["TOC 空"]

    # 解析 TOC -> 版块 + 文章 href
    toc_sections = parse_toc(book)

    # 按文件名日期兼容 . 和 -
    fname_date_re = re.compile(r"(\d{4})[-.](\d{2})[-.](\d{2})")
    fm = fname_date_re.search(epub_path.stem)

    report = [
        f"EPUB: {epub_name}",
        f"book.toc 原始项数: {len(toc_raw)}",
        f"解析版块数: {len(toc_sections)} | TOC 文章总数: {sum(len(a) for _, a in toc_sections)}",
    ]

    # H2: 至少 1 个版块含文章 href
    total_arts = sum(len(a) for _, a in toc_sections)
    if total_arts == 0:
        report.append("H2 违反: TOC 无文章链接")
        return None, [], [], [], [], report, ["TOC 无文章 href"]

    # 展平文章序列（带版块归属）
    flat = []  # [(section_name, article_title, article_href)]
    for sec_name, arts in toc_sections:
        for t, h in arts:
            flat.append((sec_name or "Unknown", t, h))
    report.append(f"展平文章序列: {len(flat)} 篇")

    # 按 spine 顺序的 document items
    ordered_docs = get_ordered_docs(book)
    report.append(f"spine document 数: {len(ordered_docs)}")
    # 列出前 5 个文件名供调试
    for i, d in enumerate(ordered_docs[:5]):
        report.append(f"  spine[{i}]: {d.get_name()}")

    # 逐篇解析
    parsed_articles = []
    skipped = []
    sec_name_to_key = {}
    sec_order = 0
    # 先建 section 表
    for sec_name, _ in toc_sections:
        if sec_name and sec_name not in sec_name_to_key:
            sec_name_to_key[sec_name] = f"sec_{sec_order}"
            sec_order += 1
    if "Unknown" not in sec_name_to_key:
        sec_name_to_key["Unknown"] = f"sec_{sec_order}"
        sec_order += 1

    for idx, (sec_name, toc_title, href) in enumerate(flat):
        start_path, start_anchor = href_to_path(href)
        # 下一篇
        if idx + 1 < len(flat):
            _, next_title, next_href = flat[idx + 1]
            next_path, next_anchor = href_to_path(next_href)
        else:
            next_path, next_anchor = None, None

        elements, err = collect_article_elements(ordered_docs, start_path, start_anchor, next_path, next_anchor)
        if err:
            report.append(f"  篇{idx} collect 警告: {err} (href={href})")
        if not elements:
            skipped.append(f"篇{idx} ({toc_title[:40]}): 无元素 {err or ''}")
            continue

        parsed = parse_article(elements, toc_title)
        if "skip" in parsed:
            skipped.append(f"篇{idx} ({toc_title[:40]}): {parsed['skip']}")
            continue
        parsed["_idx"] = idx
        parsed["_sec_name"] = sec_name
        parsed["_toc_title"] = toc_title
        parsed_articles.append(parsed)

    report.append(f"文章解析: 成功 {len(parsed_articles)} / 跳过 {len(skipped)}")
    for s in skipped:
        report.append(f"  skip: {s}")

    # 组装输出结构
    sections_out, articles_out, headings_out, images_out = [], [], [], []
    for sec_name, _ in toc_sections:
        if sec_name:
            sk = sec_name_to_key.get(sec_name)
            if sk:
                sections_out.append({"section_key": sk, "name": sec_name, "sort_order": int(sk.split("_")[1])})
    if not any(s["name"] == "Unknown" for s in sections_out):
        sk = sec_name_to_key["Unknown"]
        sections_out.append({"section_key": sk, "name": "Unknown", "sort_order": int(sk.split("_")[1])})

    sec_key_count = {}
    for a in parsed_articles:
        sec_name = a["_sec_name"]
        sec_key = sec_name_to_key.get(sec_name, sec_name_to_key["Unknown"])
        sort_order = sec_key_count.get(sec_key, 0)
        sec_key_count[sec_key] = sort_order + 1
        art_key = f"art_{a['_idx']}"
        articles_out.append({
            "article_key": art_key,
            "section_key": sec_key,
            "sort_order": sort_order,
            "column_label": a["column_label"],
            "title": a["title"],
            "subtitle": a["subtitle"],
            "teaser": a["teaser"],
            "published_at": a["published_at"],
            "location": a["location"],
            "body_blocks": a["body_blocks"],
            "body_text": a["body_text"],
            "word_count": a["word_count"],
            "content_type": a["content_type"],
        })
        for h in a["headings"]:
            headings_out.append({"article_key": art_key, **h})
        for im in a["images"]:
            ext = Path(im["src"]).suffix.lower() or ".jpg"
            images_out.append({
                "article_key": art_key,
                "sort_order": im["sort_order"],
                "zip_path": im["zip_path"],
                "fname": f"{art_key}_{im['sort_order'] + 1}{ext}",
                "caption": im["caption"],
                "role": im["role"],
            })

    # issue 元信息：文件名日期优先
    if fm:
        pub_date = f"{fm.group(1)}-{fm.group(2)}-{fm.group(3)}"
    elif articles_out and articles_out[0]["published_at"]:
        pub_date = articles_out[0]["published_at"][:10]
    else:
        pub_date = "1970-01-01"
    issue = {
        "title": f"The Economist {pub_date}",
        "pub_date": pub_date,
        "source_file": epub_name,
        "article_count": len(articles_out),
    }

    # 校验
    errors, warnings_ = [], []
    wc_list = sorted(a["word_count"] for a in articles_out) or [0]
    for a in articles_out:
        if not a["title"]:
            errors.append(f"{a['article_key']}: empty title")
        if not a["body_text"]:
            errors.append(f"{a['article_key']}: empty body")
        if not a["published_at"]:
            warnings_.append(f"{a['article_key']} ({a['title'][:40]}): no published_at (H5)")
        if not a["column_label"]:
            warnings_.append(f"{a['article_key']} ({a['title'][:40]}): no column_label")
        if not a["subtitle"]:
            warnings_.append(f"{a['article_key']} ({a['title'][:40]}): no subtitle")
        if not a["body_blocks"]:
            errors.append(f"{a['article_key']}: empty body_blocks")
    art_keys = {a["article_key"] for a in articles_out}
    for h in headings_out:
        if h["article_key"] not in art_keys:
            errors.append(f"heading orphan: {h['article_key']}")

    # 图片 zip 路径校验（用 book 的 item names）
    item_names = {it.get_name() for it in ordered_docs}
    # 图片可能在非 spine 的资源里，用 zip 全集校验更准；此处用 book.items
    all_item_names = {it.get_name() for it in book.get_items()}
    for im in images_out:
        if im["article_key"] not in art_keys:
            errors.append(f"image orphan: {im['article_key']} {im['fname']}")
        elif im["zip_path"] not in all_item_names:
            # 尝试后缀匹配
            if not any(n.endswith(im["zip_path"]) or im["zip_path"].endswith(n) for n in all_item_names):
                errors.append(f"image missing in zip: {im['zip_path']}")

    # 广告清零校验：body_text 不应含订阅广告关键词
    ad_leak = 0
    for a in articles_out:
        for line in a["body_text"].split("\n\n"):
            if AD_FIRST_RE.match(line) or WATERMARK_RE.search(line):
                ad_leak += 1
                warnings_.append(f"{a['article_key']}: 广告残留 -> {line[:50]}")

    no_caption = sum(1 for im in images_out if not im["caption"])
    lead_count = sum(1 for im in images_out if im["role"] == "lead")
    report += [
        "",
        "=== 校验报告 ===",
        f"sections={len(sections_out)} articles={len(articles_out)} "
        f"headings={len(headings_out)} images={len(images_out)} "
        f"(无图注 {no_caption}, lead 题图 {lead_count})",
        f"word_count: min={wc_list[0]} median={wc_list[len(wc_list)//2]} max={wc_list[-1]}",
        f"cartoons={sum(1 for a in articles_out if a['content_type']=='cartoon')}",
        f"广告残留: {ad_leak} 段",
        f"errors={len(errors)} warnings={len(warnings_)}",
        *[f"  ERROR {e}" for e in errors[:20]],
        *([f"  ... 共 {len(errors)} 条 ERROR"] if len(errors) > 20 else []),
        *[f"  warn  {w}" for w in warnings_[:20]],
        *([f"  ... 共 {len(warnings_)} 条 warn"] if len(warnings_) > 20 else []),
    ]
    return issue, sections_out, articles_out, headings_out, images_out, report, errors


# ---------------------------------------------------------------- 上传（复用旧版）

def load_env(path: str = ".env") -> dict:
    env = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def upload(book, issue, sections, articles, headings, images):
    """直传 Supabase: 行数据 insert, 图片从 book item 直传私有 bucket。幂等重灌。"""
    from supabase import create_client

    env = load_env()
    sb = create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    pub_date = issue["pub_date"]

    # 幂等: 同 pub_date 旧刊先删（外键级联）
    existing = sb.table("issues").select("id").eq("pub_date", pub_date).execute()
    for row in existing.data:
        sb.table("issues").delete().eq("id", row["id"]).execute()
        print(f"删除旧刊 issue id={row['id']} (级联清空子表)")

    issue_id = (
        sb.table("issues").insert({
            "title": issue["title"],
            "pub_date": pub_date,
            "source_file": issue["source_file"],
            "article_count": issue["article_count"],
        }).execute().data[0]["id"]
    )
    print(f"issues: 1 (id={issue_id}, {pub_date})")

    sec_ids = {}
    for s in sections:
        sec_ids[s["section_key"]] = sb.table("sections").insert({
            "issue_id": issue_id, "name": s["name"], "sort_order": s["sort_order"],
        }).execute().data[0]["id"]
    print(f"sections: {len(sec_ids)}")

    art_ids = {}
    fields = (
        "sort_order", "column_label", "title", "subtitle", "teaser",
        "published_at", "location", "body_blocks", "body_text", "word_count",
        "content_type",
    )
    for a in articles:
        row = {k: a[k] for k in fields}
        row["issue_id"] = issue_id
        row["section_id"] = sec_ids[a["section_key"]]
        art_ids[a["article_key"]] = sb.table("articles").insert(row).execute().data[0]["id"]
    print(f"articles: {len(art_ids)}")

    for h in headings:
        sb.table("headings").insert({
            "article_id": art_ids[h["article_key"]],
            "text": h["text"], "sort_order": h["sort_order"],
        }).execute()
    print(f"headings: {len(headings)}")

    # 图片字节从 book item 取
    name_to_item = {it.get_name(): it for it in book.get_items()}
    n = 0
    import time
    for im in images:
        # 精确 / 后缀匹配 item
        item = name_to_item.get(im["zip_path"])
        if item is None:
            for nm, it in name_to_item.items():
                if nm.endswith(im["zip_path"]) or im["zip_path"].endswith(nm):
                    item = it
                    break
        if item is None:
            print(f"  WARN: 图片未找到 {im['zip_path']}")
            continue
        storage_path = f"{pub_date}/{im['fname']}"
        ctype = mimetypes.guess_type(im["fname"])[0] or "image/jpeg"
        # 重试 3 次（网络波动时图片上传可能超时/断连）
        success = False
        for attempt in range(3):
            try:
                sb.storage.from_(BUCKET).upload(
                    storage_path, item.get_content(),
                    {"content-type": ctype, "upsert": "true"},
                )
                sb.table("images").insert({
                    "article_id": art_ids[im["article_key"]],
                    "sort_order": im["sort_order"],
                    "storage_path": storage_path,
                    "caption": im["caption"],
                    "role": im["role"],
                }).execute()
                success = True
                break
            except Exception as e:
                if attempt < 2:
                    print(f"  重试 {attempt+1}/3 {im['fname']}: {type(e).__name__}")
                    time.sleep(3)
                else:
                    print(f"  FAIL {im['fname']}: {type(e).__name__}: {e}")
        if success:
            n += 1
            if n % 25 == 0:
                print(f"images: {n}/{len(images)} ...")
    print(f"images: {n}")
    print("入库完成")


# ---------------------------------------------------------------- dump（可选）

def dump_local(book, out_dir, issue, sections, articles, headings, images):
    out = Path(out_dir)
    img_dir = out / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    pub_date = issue["pub_date"]

    (out / "issues.json").write_text(
        json.dumps([{**issue, "issue_key": pub_date}], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "sections.json").write_text(
        json.dumps(sections, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "articles.json").write_text(
        json.dumps(articles, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out / "headings.json").write_text(
        json.dumps(headings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    name_to_item = {it.get_name(): it for it in book.get_items()}
    images_local = []
    for im in images:
        item = name_to_item.get(im["zip_path"])
        if item is None:
            for nm, it in name_to_item.items():
                if nm.endswith(im["zip_path"]) or im["zip_path"].endswith(nm):
                    item = it
                    break
        if item is not None:
            (img_dir / im["fname"]).write_bytes(item.get_content())
        images_local.append({
            "article_key": im["article_key"], "sort_order": im["sort_order"],
            "file": f"images/{im['fname']}", "caption": im["caption"], "role": im["role"],
        })
    (out / "images.json").write_text(
        json.dumps(images_local, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"本地备份 -> {out}/ (五份 JSON + images/)")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="The Economist EPUB -> Supabase 一站式入库 (v3 TOC 定位)")
    ap.add_argument("epub", help="EPUB 文件路径")
    ap.add_argument("--dump", action="store_true", help="同时把 JSON+图片落 output/ 备查")
    ap.add_argument("--no-upload", action="store_true", help="只提取落 output/, 不上传")
    ap.add_argument("-o", "--out", default="output", help="落本地的目录 (默认 output/)")
    args = ap.parse_args()

    epub_path = Path(args.epub)
    if not epub_path.exists():
        sys.exit(f"找不到文件: {epub_path}")

    # extract 需要 book 对象（dump/upload 也要），统一在这里读
    try:
        book = epub.read_epub(str(epub_path), options={"ignore_ncx": False})
    except Exception as e:
        print(f"H1 违反: ebooklib 读取失败: {e}")
        sys.exit(1)

    issue, sections, articles, headings, images, report, errors = extract(book, epub_path)
    report_text = "\n".join(report)
    print(report_text)

    if errors:
        print("\n校验存在错误, 不上传。请检查上方 ERROR 项。")
        # 即使有错误，--no-upload/--dump 仍落本地备查
        if args.dump or args.no_upload:
            if issue is not None:
                dump_local(book, args.out, issue, sections, articles, headings, images)
                (Path(args.out) / "_report.txt").write_text(report_text, encoding="utf-8")
        sys.exit(1)

    if args.dump or args.no_upload:
        dump_local(book, args.out, issue, sections, articles, headings, images)
        (Path(args.out) / "_report.txt").write_text(report_text, encoding="utf-8")

    if args.no_upload:
        print("已按 --no-upload 要求跳过上传。")
        return

    print("\n=== 上传 Supabase ===")
    # 整体重试 3 次（网络波动时 upload 可能中途断，幂等可重跑）
    import time
    for attempt in range(3):
        try:
            upload(book, issue, sections, articles, headings, images)
            break
        except Exception as e:
            if attempt < 2:
                print(f"\n上传失败，重试 {attempt+1}/3: {type(e).__name__}: {e}")
                time.sleep(5)
            else:
                raise


if __name__ == "__main__":
    main()
