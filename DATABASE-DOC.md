# readbay-article-database 数据库文档

> 经济学人 EPUB 结构化入库数据库的完整交接文档。
> 本文档与数据库内的 `COMMENT ON`（Supabase Dashboard 可见）保持一致。
> 最后更新: 2026-07-26 | schema 版本: v3 | 对应代码: `ingest.py` (feat/ingest-v3 分支)

---

## 一、数据库总览

| 项 | 值 |
|---|---|
| 项目名 | readbay-article-database |
| Supabase ref | `qygqpedoqfctcopojcyt` |
| region | ap-southeast-1 |
| PostgreSQL | 17.6 |
| endpoint | `https://qygqpedoqfctcopojcyt.supabase.co` |
| 表数量 | 5 (issues / sections / articles / headings / images) |
| Storage bucket | `article-images` (私有) |
| 数据规模 | 每期约 70-80 篇文章 + 100-140 张图；6 期共 447 篇 |

**用途**：把经济学人 EPUB 逐篇提取为结构化数据（标题/副标题/正文/小标题/图片及图注/发布时间），供个人网站、未来 APP、播客制作复用。每期新 EPUB 可一键重跑入库（幂等）。

---

## 二、5 表关系图

```
issues (期刊，顶层)
  │ 1
  │
  ├─< sections (版块)
  │     │ 1
  │     │
  │     └─< articles (文章，主表) ─< headings (小标题)
  │                 │
  │                 └─< images (图片)
  │
  └─ (article_count 冗余字段，等于该期 articles 行数)
```

- `issues` 1—N `sections` 1—N `articles` 1—N `headings` / `images`
- 所有外键 `ON DELETE CASCADE`：删期刊 → 级联删版块 → 文章 → 小标题/图片
- `articles.body_blocks` 内的 `{type:"img",ref:N}` 通过 `ref` 引用同文章 `images.sort_order`（逻辑外键，无 DB 约束）

---

## 三、各表字段说明

> 以下描述与数据库内 `COMMENT ON` 完全一致。Supabase Dashboard → Table Editor 可直接看到。

### 3.1 issues — 期刊表

每期经济学人 EPUB 对应一行。顶层表。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | bigint | PK, GENERATED ALWAYS AS IDENTITY | 主键（Supabase 推荐写法，非 uuid） |
| title | text | NOT NULL | 期刊标题，格式 "The Economist YYYY-MM-DD" |
| pub_date | date | NOT NULL | 出版日期（从文件名解析，兼容 `-` 和 `.` 分隔）。**幂等重灌依据**：同 pub_date 旧刊先 delete 再 insert |
| source_file | text | NOT NULL | 源 EPUB 文件名，溯源用 |
| article_count | integer | NOT NULL DEFAULT 0 | 本期文章数（经济数据页等零正文切片已 skip，不计入） |
| created_at | timestamptz | NOT NULL DEFAULT now() | 记录创建时间 |

### 3.2 sections — 版块表

版块来自 EPUB 的 TOC（`ebooklib.book.toc`），每期约 19-21 个。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | bigint | PK, identity | 主键 |
| issue_id | bigint | NOT NULL, FK→issues, CASCADE | 所属期刊 |
| name | text | NOT NULL | 版块名（来自 TOC，如 Leaders/Britain/United States/The world this week/Finance & economics） |
| sort_order | integer | NOT NULL | 版块在期刊内的顺序，从 0 开始 |

### 3.3 articles — 文章主表（核心）

经济学人每期约 70-80 篇。`body_blocks` 是承重字段（行内级 IR），`body_text` 管检索/TTS。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | bigint | PK, identity | 主键 |
| issue_id | bigint | NOT NULL, FK→issues, CASCADE | 所属期刊 |
| section_id | bigint | NOT NULL, FK→sections, CASCADE | 所属版块 |
| sort_order | integer | NOT NULL | 文章在版块内的顺序，从 0 开始 |
| column_label | text | NULL | 栏目标签（文章开头红字短文本，位置1）。专栏文章是专栏名（Bartleby/Bagehot/Charlemagne/Free Exchange/Buttonwood 等）；"The world this week" 版块为版块名；少数文章无 → NULL |
| title | text | NOT NULL | 主标题（网络版为准，铁律）。位置2，h1 或 p 标签都接受 |
| subtitle | text | NULL | 副标题（位置3）。少数文章无 → NULL（如 The world this week 的 Politics/Business 简讯） |
| teaser | text | NULL | 索引页导语（TOC 索引页 " :: " 后半截）。**v3 暂未提取，全 NULL，字段预留** |
| published_at | timestamptz | NULL | 发布时间。三格式日期行正则解析：英文 `Jul 09, 2026 05:22 AM`、中文 `6月 18, 2026 03:17 上午`、序数英文 `June 11th 2026`。源站未给时区按 UTC (+00:00)。少数文章无 → NULL |
| location | text | NULL | 地点（日期行 `|` 后的地点，如 Singapore/BEIJING）。约 33% 文章有 |
| body_blocks | jsonb | NOT NULL | **行内级结构化正文 IR**（承重字段，见 §四）。支撑网站/APP 按原排版 1:1 还原 |
| body_text | text | NOT NULL | 清洗后纯文本（段落 `\n\n` 分隔），管全文搜索/播客 TTS。已执行清洗：去 navbar/首字下沉合并/■截断/剥水印/剥推广语/交叉引用留文字弃 URL/空白归一/NFC |
| word_count | integer | NOT NULL DEFAULT 0 | 英文词数（正则 `[A-Za-z0-9''-]+`） |
| content_type | text | NOT NULL DEFAULT 'article' | 内容类型。当前值：`article`（普通文章）/ `cartoon`（漫画，靠 title 以 "cartoon" 开头识别）。无 CHECK 约束，未来可扩展 `brief`/`data` |

### 3.4 headings — 小标题表

文章内的小标题（源 EPUB 中的 h2-h6 标签）。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | bigint | PK, identity | 主键 |
| article_id | bigint | NOT NULL, FK→articles, CASCADE | 所属文章 |
| text | text | NOT NULL | 小标题文本（已清洗） |
| sort_order | integer | NOT NULL | 小标题在文章内的顺序，从 0 开始 |

> 注：部分期（如 07-04 经济学人原生 EPUB）文章无 h 标签 → 该期 headings 为 0，属正常。

### 3.5 images — 图片表

图片二进制存私有 Storage bucket `article-images`，表内存 `storage_path`。每期约 100-140 张。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | bigint | PK, identity | 主键 |
| article_id | bigint | NOT NULL, FK→articles, CASCADE | 所属文章 |
| sort_order | integer | NOT NULL | 图片在文章内的顺序，从 0 开始；lead 题图先编号，figure 内文图按文档序 |
| storage_path | text | NOT NULL | Storage 路径，格式 `{pub_date}/{article_key}_{sort_order+1}{ext}`，如 `2026-07-11/art_5_1.jpg`。私有 bucket，访问需签 URL |
| caption | text | NULL | 图注（双来源取非空：`img.title ∪ img.alt ∪ 图下 div/p 文本`）。无则 NULL（铁律：每张图只携带自己的图注，绝不串借）。约 30-40% 图片无图注 |
| role | text | NOT NULL DEFAULT 'figure' | 图片角色：`lead`=题图（头部区块内的 img，正文开始前）/ `figure`=内文图（正文中）。body_blocks 的 `{type:"img",ref}` 通过此 sort_order 关联 |

---

## 四、body_blocks JSON 结构定义（承重字段）

`articles.body_blocks` 是行内级结构化正文 IR（中间表示），支撑网站/APP 按原排版 1:1 还原。jsonb 类型，无大小限制。

### 4.1 顶层结构

```json
[
  { "type": "p", "runs": [<run>, ...], "dropcap": true },
  { "type": "h", "runs": [<run>, ...] },
  { "type": "img", "ref": <images.sort_order> }
]
```

| 块类型 type | 含义 | 来源 |
|---|---|---|
| `p` | 段落 | 正文 `<p>`，可带 `dropcap` 首字下沉标记 |
| `h` | 小标题 | 源 EPUB 的 h2-h6，同步写一份到 headings 表 |
| `img` | 图片引用 | 引用同文章 `images.sort_order`（逻辑外键） |

### 4.2 run 类型（块内行内片段）

| t | 含义 | 来源 | 渲染建议 |
|---|---|---|---|
| `text` | 纯文本 | 默认文本节点 | 直接输出 |
| `b` | 粗体 | `<b>`/`<strong>` 或 CSS font-weight:bold | `<strong>` |
| `i` | 斜体 | `<i>`/`<em>` 或 CSS font-style:italic | `<em>` |
| `sc` | 小型大写 | 经济学人标志性排版（大写 + CSS small-caps） | `<span style="font-variant:small-caps">` |
| `a` | 超链接 | `<a href>`，交叉引用 | `<a href>`，留文字弃相对 URL 时需解析 |

run 对象：`{ "t": "text|b|i|sc|a", "x": "文本内容", "href": "..."（仅 a 类型）}`。同类型相邻 run 合并。

### 4.3 消费端设计

- **网站/APP**：写一个 run 渲染器（switch 函数：b→strong/i→em/sc→span/a→a，所有文章复用一个组件），按 body_blocks 顺序渲染
- **播客/TTS**：直接用 `body_text`，不碰 runs
- **全文搜索**：用 `body_text`（可加 PostgreSQL `tsvector` 索引）
- body_blocks 是可移植 IR：加新消费端 = 写新渲染器，数据不动

---

## 五、安全策略（RLS）

| 表 | RLS | policy | 访问 |
|---|---|---|---|
| issues / sections / articles / headings / images | 全部 ENABLE | **不建任何 policy** | anon/authenticated **默认全拒**；service_role(loader) 与 postgres(MCP) 绕过 RLS 正常读写 |

**设计意图**：数据库只供自己用（个人网站/APP 后端），不对外公开。若未来要开放部分读权限，按需建 SELECT policy（如 `using (true)` 公开读 articles）。

**密钥**：`service_role` 在项目内 `.env`（`SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`），**勿提交 git、勿分享**。

---

## 六、数据流（EPUB → 数据库）

```
经济学人 EPUB 文件
       │
       ▼  python ingest.py <epub>
┌──────────────────────────────────────────┐
│  ingest.py (v3: TOC 定位 + 位置顺序提取) │
│                                          │
│  1. ebooklib 读 book.toc → 版块+文章href │
│  2. 文章范围 = 当前href → 下一href（跨文件）│
│  3. 范围内按 6 位置 layout 提取字段：     │
│     位置1 栏目标签 → column_label         │
│     位置2 主标题   → title                │
│     位置3 副标题   → subtitle             │
│     位置4 日期行   → published_at+location│
│     位置5 题图     → lead image           │
│     位置6+ 正文    → body_blocks+body_text│
│  4. ■ 截断（剥广告/水印）                 │
│  5. 基本清洗 + 入库                       │
│  6. 跳过零正文切片（经济数据页）          │
└──────────────────────────────────────────┘
       │
       ▼  service_role 直传
┌──────────────────────────────────────────┐
│  Supabase (readbay-article-database)     │
│  - 5 表 insert（行数据）                  │
│  - Storage bucket article-images upload  │
│    （图片二进制，私有，upsert 覆盖同路径）│
└──────────────────────────────────────────┘
```

**用法**：
- `python ingest.py <epub>` — 提取并直接上传（默认，不落本地文件）
- `python ingest.py <epub> --no-upload` — 只提取到 `out_v3_<date>/`，不上传（本地验证用）
- `python ingest.py <epub> --dump` — 同时落本地备份 + 上传

---

## 七、幂等重灌机制

同一期重跑不会产生重复数据：

1. `ingest.py` 上传时先 `select id from issues where pub_date = ?`
2. 旧刊 `delete from issues where id = ?`（外键 CASCADE 级联删 sections/articles/headings/images 行）
3. Storage bucket 同路径图片 `upsert: true` 覆盖
4. 重新 insert 新数据

→ 同一 pub_date 可无限次重跑，结果幂等。

---

## 八、查询示例

```sql
-- 查某期所有文章（含版块名）
select s.name as section, a.sort_order, a.title, a.subtitle, a.word_count
from articles a
join sections s on s.id = a.section_id
join issues i on i.id = a.issue_id
where i.pub_date = '2026-07-11'
order by s.sort_order, a.sort_order;

-- 全文搜索（body_text）
select title, left(body_text, 100) as preview
from articles
where body_text ilike '%NATO%'
order by published_at desc;

-- 取一篇文章的 body_blocks（渲染用）
select body_blocks from articles where id = 123;

-- 取某文章所有图（带图注和角色）
select sort_order, storage_path, caption, role
from images where article_id = 123 order by sort_order;

-- 统计每期文章数
select pub_date, article_count from issues order by pub_date;

-- 取某专栏所有文章（如 Bagehot）
select i.pub_date, a.title from articles a
join issues i on i.id = a.issue_id
where a.column_label = 'Bagehot'
order by i.pub_date;
```

---

## 九、交接说明（接手者必读）

### 9.1 如何理解这个数据库

1. **先看本文档** §一~§四：了解总览、5 表关系、字段语义、body_blocks 结构
2. **看 Supabase Dashboard**：打开项目 → Table Editor，每个表/列的描述字段即 `COMMENT ON`（与本文档 §三一致）
3. **看 `schema.sql`**：完整 DDL（建表语句 + COMMENT ON + 索引 + RLS）
4. **看 `ingest.py`**：数据怎么来的（v3 提取逻辑）
5. **看 `VERIFY-REPORT.md`**：6 本 EPUB 的本地验证结果（篇数/广告清零/字段覆盖）
6. **看 `PLAN-v3.md`**（在 `.workbuddy/memory/`）：方案设计依据（4 个共性、提取流程、清洗规则）

### 9.2 如何重灌一期

```bash
# 1. 把新 EPUB 放到项目根
# 2. 本地验证（不上传）
python ingest.py "TE-2026-07-25-EPUB.epub" --no-upload -o out_v3_0725
# 3. 检查 out_v3_0725/_report.txt，确认篇数/广告清零/errors=0
# 4. 确认后正式入库
python ingest.py "TE-2026-07-25-EPUB.epub"
```

### 9.3 如何重建整个数据库（推倒重来）

```bash
# 通过 Supabase MCP apply_migration 执行 schema.sql
# 或 Dashboard SQL Editor 粘贴 schema.sql 运行
# 然后 6 本依次重灌
for f in TheEconomist.2026.06.13.epub TE-2026-06-20-EPUB.epub TE-2026-06-27-EPUB.epub \
         "The Economist 2026-07-04.epub" TE-2026-07-11-EPUB.epub "The Economist-2026-07-18.epub"; do
  python ingest.py "$f"
done
```

### 9.4 字段类型说明（防"文章太长存不下"疑问）

PostgreSQL 的 `text` 和 `jsonb` 类型**无长度限制**（最大约 1GB）。经济学人最长文章约 7000 词 / 40KB，离 1GB 差 2.5 万倍。即使未来入库 100 万字长文也完全够。**不存在"文章太长存不下"的问题。**

### 9.5 已知数据特征（非 bug）

- **07-04 headings=0**：该期经济学人原生 EPUB 文章无 h2-h6 小标题标签，排版差异，非提取 bug
- **06-13 Economic data 页**：v3 已统一 skip（和其他期一致）
- **Politics/Business 无 subtitle**：The world this week 版块的两条简讯本就无副标题
- **约 33% 文章无 location**：日期行无 `|` 地点
- **约 30-40% 图片无 caption**：源 EPUB 的 img 无 title/alt/图下文本，存 NULL（铁律：绝不串借图注）
- **teaser 全 NULL**：v3 暂未提取索引页导语，字段预留

---

## 十、相关文件

| 文件 | 用途 |
|---|---|
| `schema.sql` | 完整 DDL（建表 + COMMENT ON + 索引 + RLS + bucket） |
| `ingest.py` | EPUB → Supabase 一站式入库脚本（v3） |
| `DATABASE-DOC.md` | 本文档（数据库外文档） |
| `VERIFY-REPORT.md` | 6 本 EPUB 本地验证报告 |
| `PLAN-v3.md` | 提取方案设计依据（`.workbuddy/memory/`） |
| `.env` | Supabase 密钥（SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，勿提交） |
