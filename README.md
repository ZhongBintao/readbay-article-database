# 经济学人 EPUB → Supabase 结构化入库

> 项目索引。开始任务前先读 [DATABASE-DOC.md](DATABASE-DOC.md)（数据库文档）和 `.workbuddy/memory/PLAN-v3.md`（提取方案）。

## 项目目标

经济学人 EPUB 逐篇提取为结构化数据（标题/副标题/正文/小标题/图片及图注/发布时间），存入 Supabase 项目 **readbay-article-database**。纯代码管线（EbookLib + BeautifulSoup），不依赖 AI/LLM。后期用途：按原排版重构文章 → 个人网站 / APP / 播客。

## 文件索引

### 代码（项目根）
| 文件 | 用途 |
|---|---|
| `ingest.py` | 主程序：EPUB → 提取 → 直传 Supabase（一键入库，幂等，含双重重试） |
| `render_article.py` | 还原文章成 HTML 预览（验证提取质量，支持 `--titles` 指定 + 随机补） |
| `schema.sql` | 数据库建表 DDL（5 表 + 全 COMMENT ON + RLS + bucket，详见下文） |

### 文档
| 文件 | 用途 |
|---|---|
| `DATABASE-DOC.md` | 数据库交接文档（总览/5 表关系/字段说明/body_blocks JSON 定义/RLS/数据流/查询示例/交接说明） |
| `.workbuddy/memory/PLAN-v3.md` | v3 提取方案定稿（4 共性/6 位置 layout/■截断/清洗规则/假设清单） |
| `.workbuddy/memory/MEMORY.md` | 项目长期笔记（铁律/目标/基础设施/进度） |

### 配置
| 文件 | 用途 |
|---|---|
| `.env` | Supabase 密钥（SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）。**禁止删除、禁止提交 git、禁止分享**（service_role 最高权限） |
| `.gitignore` | 排除 .env / out_*/ / __pycache__/ / *.pyc |

## 数据库现状

- Supabase 项目：readbay-article-database（ref `qygqpedoqfctcopojcyt`，ap-southeast-1，PG 17.6）
- 5 表：issues / sections / articles / headings / images
- 已入库 6 期：issues 6 / sections 123 / articles 446 / headings 151 / images 699
- Storage bucket：`article-images`（私有，699 文件）
- RLS 全开无 policy（anon/authenticated 默认全拒，service_role 绕过）

## 用法

### 入库新一期
```bash
python ingest.py "新期.epub"              # 一键入库（幂等：同 pub_date 先删后插）
```

### 还原预览（验证提取质量）
```bash
python render_article.py                  # 随机 5 篇还原 HTML
python render_article.py --titles "kw1|kw2"  # 指定文章（title 模糊匹配）+ 随机补到 5 篇
```

### 重建数据库（推倒重来）
通过 Supabase MCP `apply_migration` 执行 `schema.sql`，或 Dashboard SQL Editor 粘贴运行。开头 DROP CASCADE = 整库重建。

## 数据安全

- `.env` 的 service_role 最高权限，禁提交 git / 分享
- RLS 全开无 policy = anon/authenticated 默认全拒
- 私有 bucket，图片访问需 service_role 签 URL

## 交接指引

1. 读本文件了解项目全貌
2. 读 `DATABASE-DOC.md` 了解数据库设计（5 表/字段/body_blocks/数据流）
3. 读 `.workbuddy/memory/PLAN-v3.md` 了解提取方案（4 共性/6 位置/■截断）
4. 打开 Supabase Dashboard → Table Editor（表/列 COMMENT ON 直接可见）
5. 看 `ingest.py` 了解数据怎么来的（v3: TOC 定位 + 6 位置提取 + ■截断 + 双重重试）

