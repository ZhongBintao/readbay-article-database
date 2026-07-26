# 项目长期笔记 — 经济学人结构化入库

## 项目铁律（用户明确要求，2026-07-25）

- **所有代码、配置、密钥文件一律写入本项目文件夹**（Desktop/subtract-file/），不动电脑其他任何位置
- 数据库只存干净纯文本，禁止 HTML 入库；缺失字段存 NULL
- 一律以 EPUB 网络版标题为准

## 目标

经济学人 EPUB 逐篇提取为结构化数据（标题/副标题/正文/小标题/图片及图注/发布时间），存入 Supabase 新项目 **readbay-article-database**，供检索复用；每期新 EPUB 可重跑入库。纯代码提取（EbookLib+BeautifulSoup），不用 AI API。

## 用户后期用途（21:53 明确，决定存储颗粒度）

- **按原排版重构文章** → 放个人网站、未来 APP、制作播客
- → body_blocks 不是可选而是**承重字段**；且"按原排版"要求块结构可能需保留行内格式（粗体/斜体/链接），与铁律#2"禁 HTML"存在张力，待用户定颗粒度

## 基础设施（已就绪）

- Supabase 项目：readbay-article-database，ref `qygqpedoqfctcopojcyt`，ap-southeast-1，PG 17.6
- endpoint：`https://qygqpedoqfctcopojcyt.supabase.co`
- 密钥：service_role 在项目内 `.env`（SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）
- Agent 操控通道：Supabase 官方 MCP（`https://mcp.supabase.com/mcp`，已 Trust+OAuth，可建表/执行 SQL）；Storage 无上传工具，图片走 Python loader
- **同账号另有旧项目** ZhongBintao's Project（ref xklhwtxtwbpjmdeqxmyv）：用户在跑的杂志中文摘要管线（public.articles 180 行），**不要动**
- Python venv：`~/.workbuddy/binaries/python/envs/default`（beautifulsoup4/lxml/pypdf/pillow/pdfplumber；loader 需补装 supabase 客户端）
- GitHub 仓库：`ZhongBintao/readbay-article-database`（https://github.com/ZhongBintao/readbay-article-database，公开）。已建好 + origin remote 已加，但 push 因 tun 代理 502 失败（3 次）。本地 main 已就绪（8 文件），网络好时 `git push -u origin main` 即可

## 数据规模（2026-07-11 刊）

20 版块、75 篇、135 图；舍弃经济数据页，入库 74 篇 + 约 131 图；5 表：issues/sections/articles/headings/images；bucket：article-images

## 当前进度

**方案定稿**（2026-07-26）——**完整细节见同目录 `PLAN-v3.md`**，核心：
- 用 EPUB 规范级保证（TOC）+ 杂志编辑惯例（首页固定 layout + ■ 结尾）做通用方案
- 共性1：ebooklib 读 `b.toc` → 统一"版块 Section → 文章 Link(含 href)"（6 本 epub 验证）
- 共性2：文章第一页固定 6 位置 layout（栏目标签→主标题→副标题→日期→题图→正文），靠位置+文本特征识别，**不靠颜色/class**
- 共性3：正文以 ■ 结尾（U+25A0，6 本都有）；■ 是清洗信号不是边界信号
- 共性4：■ 之后到下一篇文章之间是广告/水印/分隔，■ 截断即可清除
- 日期正则支持三格式（英文/中文/序数英文），文件名日期兼容 `.` 分隔
- schema 已就绪（项目根 schema.sql）：5表+body_blocks jsonb+images.role+删kicker+teaser
- 用户已授权整库推倒重建：无兼容包袱
- 代码现状：**项目已完成 + 文件整理完成**（feat/ingest-v3 分支）。最终保留文件：ingest.py（v3 主程序，TOC定位+6位置提取+■截断+经济数据页skip+图片normpath+双重重试）+ render_article.py（还原HTML预览，支持--titles）+ schema.sql（5表+全COMMENT ON中英双语+4处not null）+ DATABASE-DOC.md（交接文档）+ README.md（项目索引）+ MEMORY.md + PLAN-v3.md。6 本入库 Supabase：issues 6 / sections 123 / articles 446 / headings 151 / images 699，Storage 699 文件一一对应，广告全清零。已删过程文件（SCHEMA-DISCUSSION/VERIFY-REPORT/cleanup_storage/日志）
- 实施顺序：~~重写 ingest.py 为 v3~~ ✓ → ~~6 期本地验证~~ ✓ → ~~修 06-13+06-20+column_label+caption~~ ✓ → ~~schema 全COMMENT ON+DATABASE-DOC~~ ✓ → ~~drop 重建 DB + 重灌 6 期~~ ✓ → ~~还原验证+修复+重灌~~ ✓ → ~~文件整理~~ ✓ → 项目交付
- 用法 `python ingest.py <epub>` 一键入库（幂等：同 pub_date 先删后插）；`python render_article.py --titles "kw"` 还原预览
