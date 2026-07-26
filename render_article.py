#!/usr/bin/env python3
"""render_article.py — 从 Supabase 随机挑 5 篇文章还原成 HTML 预览

用法:
    python render_article.py              # 随机挑 5 篇渲染到 rendered_articles/index.html
    python render_article.py --seed 42    # 固定随机种子（可复现）

按经济学人 layout 顺序还原: 栏目标签→主标题→副标题→日期行→题图→正文(body_blocks)
图片走 Storage 签 URL 真显示（私有 bucket，service_role 签发，1 小时有效）
"""
import html
import random
import sys
from pathlib import Path

from supabase import create_client

BUCKET = 'article-images'


def load_env(path='.env'):
    env = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


def sign_url(sb, path):
    """签 Storage 私有文件 URL（1 小时有效）"""
    try:
        r = sb.storage.from_(BUCKET).create_signed_url(path, 3600)
        # supabase-py 不同版本返回类型不同，兼容处理
        if isinstance(r, dict):
            return r.get('signedURL') or (r.get('data') or {}).get('signedUrl', '')
        return getattr(r, 'signed_url', '') or getattr(r, 'signedURL', '')
    except Exception as e:
        print(f"  WARN 签 URL 失败 {path}: {e}")
        return ''


def render_runs(runs):
    """渲染行内 runs -> HTML 字符串"""
    out = []
    for r in runs:
        t = r.get('t', 'text')
        x = html.escape(r.get('x', ''))
        if t == 'text':
            out.append(x)
        elif t == 'b':
            out.append(f'<strong>{x}</strong>')
        elif t == 'i':
            out.append(f'<em>{x}</em>')
        elif t == 'sc':
            out.append(f'<span class="sc">{x}</span>')
        elif t == 'a':
            # 交叉引用：相对路径点不开，保留文字 + 虚线下划线标注
            out.append(f'<span class="xref" title="交叉引用">{x}</span>')
    return ''.join(out)


def render_block(block, images_map):
    """渲染一个 body_block -> HTML"""
    ty = block.get('type')
    if ty == 'p':
        runs_html = render_runs(block.get('runs', []))
        if block.get('dropcap') and runs_html:
            # 首字下沉：首字符包 dropcap span（注意跳过开头的 HTML 标签）
            if runs_html[0] == '<':
                # 首字符是标签开头（如 <strong>），找标签结束后的首字符
                gt = runs_html.find('>')
                if gt > 0 and gt + 1 < len(runs_html):
                    first = runs_html[:gt + 2]
                    rest = runs_html[gt + 2:]
                    runs_html = f'{first[:gt+1]}<span class="dropcap">{first[gt+1:gt+2]}</span>{first[gt+2:]}{rest}'
            else:
                runs_html = f'<span class="dropcap">{runs_html[0]}</span>{runs_html[1:]}'
        return f'<p>{runs_html}</p>'
    elif ty == 'h':
        runs_html = render_runs(block.get('runs', []))
        return f'<h3>{runs_html}</h3>'
    elif ty == 'img':
        ref = block.get('ref')
        im = images_map.get(ref)
        if im and im.get('url'):
            cap = html.escape(im.get('caption') or '')
            return (f'<figure class="inline"><img src="{im["url"]}" alt="{cap}">'
                    f'<figcaption>{cap}</figcaption></figure>')
        return '<figure class="inline missing"><div>[图片缺失]</div></figure>'
    return ''


def render_article(art, images, issue):
    """渲染一篇文章 -> HTML（按经济学人 layout 顺序）"""
    images_map = {im['sort_order']: im for im in images}
    parts = ['<article class="article">']

    # 期标记
    parts.append(f'<div class="issue-marker">期: {issue["pub_date"]} | source: {issue["title"]}</div>')

    # 位置1 栏目标签（红字）
    if art.get('column_label'):
        parts.append(f'<div class="column-label">{html.escape(art["column_label"])}</div>')

    # 位置2 主标题
    parts.append(f'<h1 class="title">{html.escape(art["title"])}</h1>')

    # 位置3 副标题
    if art.get('subtitle'):
        parts.append(f'<h2 class="subtitle">{html.escape(art["subtitle"])}</h2>')

    # 位置4 日期行（published_at | location）
    date_str = (art.get('published_at') or '')[:10]
    loc = art.get('location') or ''
    if date_str or loc:
        dl = date_str
        if loc:
            dl += f' | {loc}' if dl else loc
        parts.append(f'<div class="dateline">{html.escape(dl)}</div>')

    # 位置5 题图（lead image）
    lead_imgs = [im for im in images if im.get('role') == 'lead']
    if lead_imgs:
        im = lead_imgs[0]
        if im.get('url'):
            cap = html.escape(im.get('caption') or '')
            parts.append(f'<figure class="lead"><img src="{im["url"]}" alt="{cap}">'
                         f'<figcaption>{cap}</figcaption></figure>')

    # 位置6+ 正文（body_blocks）
    parts.append('<div class="body">')
    for block in (art.get('body_blocks') or []):
        parts.append(render_block(block, images_map))
    parts.append('</div>')

    # 元信息（验证用）
    parts.append(f'<div class="meta">article_id={art["id"]} | word_count={art["word_count"]} '
                 f'| content_type={art["content_type"]} | images={len(images)} '
                 f'(lead {len(lead_imgs)}, figure {len(images) - len(lead_imgs)})</div>')
    parts.append('</article>')
    return '\n'.join(parts)


CSS = """
body { font-family: Georgia, 'Times New Roman', serif; max-width: 820px; margin: 0 auto;
       padding: 24px; color: #1a1a1a; background: #fafafa; }
h1.page-title { font-size: 24px; color: #e3120b; border-bottom: 3px solid #e3120b;
                padding-bottom: 12px; margin-bottom: 32px; }
.issue-marker { color: #999; font-size: 12px; margin-top: 48px; }
article.article { background: #fff; padding: 32px 40px; margin: 16px 0;
                  box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-radius: 2px; }
.column-label { color: #e3120b; font-weight: bold; text-transform: uppercase;
                font-size: 13px; letter-spacing: 0.5px; margin-bottom: 6px; }
h1.title { font-size: 30px; line-height: 1.2; margin: 4px 0 10px; color: #111; }
h2.subtitle { font-size: 18px; color: #555; font-weight: normal; font-style: italic;
              margin: 0 0 12px; line-height: 1.4; }
.dateline { color: #888; font-size: 13px; margin: 0 0 20px; }
figure.lead { margin: 0 0 24px; }
figure.lead img { width: 100%; display: block; }
figure.inline { margin: 20px 0; }
figure.inline img { width: 100%; display: block; }
figure.inline.missing div { background: #eee; padding: 40px; text-align: center;
                             color: #999; font-size: 13px; }
figcaption { font-size: 12px; color: #666; margin-top: 6px; line-height: 1.4;
              font-style: italic; }
.body p { line-height: 1.75; margin: 14px 0; text-align: justify; font-size: 16px; }
.body h3 { font-size: 17px; margin: 22px 0 8px; color: #222; }
.dropcap { float: left; font-size: 3.4em; line-height: 0.82; padding: 6px 8px 0 0;
            font-weight: bold; color: #111; }
.sc { font-variant: small-caps; letter-spacing: 0.5px; }
.xref { text-decoration: underline dotted; cursor: help; color: #444; }
.meta { font-size: 11px; color: #999; margin-top: 32px; border-top: 1px dashed #ddd;
        padding-top: 10px; font-family: monospace; }
hr { border: none; border-top: 1px solid #eee; margin: 8px 0; }
"""


def main():
    import argparse
    ap = argparse.ArgumentParser(description='从 Supabase 还原文章成 HTML')
    ap.add_argument('--titles', help='指定文章 title 模糊匹配（| 分隔），加随机补到 5 篇')
    ap.add_argument('--seed', type=int, help='随机种子（可复现）')
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    env = load_env()
    sb = create_client(env['SUPABASE_URL'], env['SUPABASE_SERVICE_ROLE_KEY'])

    picked = []  # [(issue, article_id, title)]
    target = 5

    # 1. 指定 title 模糊匹配（--titles "kw1|kw2"）
    if args.titles:
        for kw in args.titles.split('|'):
            kw = kw.strip()
            if not kw:
                continue
            resp = sb.table('articles').select('id, title, issue_id').ilike('title', f'%{kw}%').limit(1).execute()
            if resp.data:
                a = resp.data[0]
                iss = sb.table('issues').select('id, pub_date, title').eq('id', a['issue_id']).execute().data[0]
                picked.append((iss, a['id'], a['title']))
                print(f"  [指定] {iss['pub_date']}: id={a['id']} {a['title'][:50]}")
            else:
                print(f"  [指定] 未找到: {kw}")

    # 2. 随机补到 target 篇（从不同期挑）
    if len(picked) < target:
        issues = sb.table('issues').select('id, pub_date, title').execute().data
        already_issues = {p[0]['id'] for p in picked}
        candidates = [i for i in issues if i['id'] not in already_issues]
        random.shuffle(candidates)
        for iss in candidates:
            if len(picked) >= target:
                break
            arts = sb.table('articles').select('id, title').eq('issue_id', iss['id']).execute().data
            if arts:
                a = random.choice(arts)
                picked.append((iss, a['id'], a['title']))
                print(f"  [随机] {iss['pub_date']}: id={a['id']} {a['title'][:50]}")

    print(f"共挑 {len(picked)} 篇")

    # 3. 取每篇完整数据 + images + 签 URL + 渲染
    articles_html = []
    for iss, aid, _ in picked:
        art = sb.table('articles').select('*').eq('id', aid).execute().data[0]
        imgs = sb.table('images').select('*').eq('article_id', aid).order('sort_order').execute().data
        for im in imgs:
            im['url'] = sign_url(sb, im['storage_path'])
        articles_html.append(render_article(art, imgs, iss))

    # 4. 拼 index.html
    body_parts = [f'<h1 class="page-title">经济学人文章还原预览（随机 {len(articles_html)} 篇）</h1>']
    body_parts.append('<div class="issue-marker">按经济学人 layout 还原: '
                      '栏目标签→标题→副标题→日期→题图→正文。图片走 Storage 签 URL（1 小时有效）。</div>')
    for h in articles_html:
        body_parts.append(h)
        body_parts.append('<hr>')

    doc = (f'<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>经济学人文章还原预览</title><style>{CSS}</style></head>'
           f'<body>{"".join(body_parts)}</body></html>')

    out_dir = Path('rendered_articles')
    out_dir.mkdir(exist_ok=True)
    (out_dir / 'index.html').write_text(doc, encoding='utf-8')
    print(f"\n生成 {out_dir}/index.html，{len(articles_html)} 篇文章")


if __name__ == '__main__':
    main()
