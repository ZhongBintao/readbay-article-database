# 经济学人 EPUB → Supabase 提取方案（2026-07-26 定稿）

> 本文件是唯一执行方案。schema.sql 已就绪（项目根），本文件管提取逻辑。

---

## 1. 目标与铁律

把经济学人 EPUB 逐篇提取为结构化数据（标题/副标题/正文/小标题/图片及图注/发布时间），存入 Supabase 项目 **readbay-article-database**（ref `qygqpedoqfctcopojcyt`），每期新 EPUB 可一键重跑入库。

**铁律**：
1. 所有代码/配置/密钥只写项目文件夹（Desktop/subtract-file/），不动电脑其他任何位置
2. 数据库只存干净纯文本，禁止 HTML 字符串入库；缺失字段存 NULL，不编造
3. 一律以 EPUB 网络版标题为准
4. 纯代码管线，禁止手工修补数据；生产运行零 LLM
5. 图片与图注必须一一对应（每张图只携带自己的图注，无则 NULL，绝不串借）

**铁律 #2 调整**："禁 HTML 入库" 重新定义为 **"禁原始 HTML 字符串，允许结构化 runs"**——body_blocks 存的是干净 JSON 节点，不是 HTML 源码；消费端自己决定怎么渲染。

## 2. 用户后期用途（决定存储颗粒度）

- **按原排版重构文章** → 放个人网站、未来 APP、制作播客
- → body_blocks 是**承重字段**（行内级 runs，支撑 1:1 还原）；body_text 管全文搜索/播客 TTS；headings/images 表管结构化查询

## 3. 基础设施（已就绪）

- Supabase 项目：readbay-article-database，ref `qygqpedoqfctcopojcyt`，ap-southeast-1，PG 17.6
- endpoint：`https://qygqpedoqfctcopojcyt.supabase.co`
- 密钥：service_role 在项目内 `.env`（SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）
- Agent 操控通道：Supabase 官方 MCP（可建表/执行 SQL）；Storage 无上传工具，图片走 Python loader
- **同账号另有旧项目** ZhongBintao's Project（ref xklhwtxtwbpjmdeqx）：用户在跑的杂志中文摘要管线，**不要动**
- Python venv：`~/.workbuddy/binaries/python/envs/default`（beautifulsoup4/lxml/ebooklib/supabase-py 已装）
- 用户已授权整库推倒重建：无兼容包袱，实施时 drop 重建 + 全部重灌

## 4. 数据规模（参考）

每期约 20 版块、70-75 篇、110-140 图；舍弃经济数据页（无正文段）。5 表：issues/sections/articles/headings/images；bucket：article-images。

## 5. 经济学人文章的两个真共性（跨 6 本 epub 验证）

> 验证样本：07-11/07-18/07-04/06-20/06-27/06-13，覆盖三种 EPUB 来源（Calibre 英文/中文、920.im、经济学人原生）。

### 共性 1：EPUB 必有 TOC，且 TOC 给文章 href

- EPUB 规范强制要求（EPUB2=`toc.ncx`、EPUB3=`nav.xhtml`）
- `ebooklib.read_epub(path).toc` → 统一输出"版块 Section → 文章 Link(含 href)"
- TOC 树深度都是 2 层（版块→文章），ebooklib 已归一化，三版式输出结构一致
- **TOC 是出版方钦定的文章边界**，比任何启发式都可靠

### 共性 2：文章第一页固定 6 位置 layout（不靠颜色/class）

文章开头都是这个顺序：

```
位置1 短文本（非粗非斜）   → 栏目标签    "China | Chaguan" / "Free Exchange" / "Geopolitics"
位置2 长文本              → 主标题      （h1 或 p 都接受，不能只认 h1）
位置3 中等文本            → 副标题      （有的斜体有的不斜体，靠位置兜底）
位置4 匹配日期正则        → 发布日期    多格式见 §7
位置5 含 img 的块         → 题图        （lead）
位置6+ 段落               → 正文        （含首字下沉）
```

**识别靠**：位置顺序 + 文本特征（短/长/日期正则）
**不靠**：颜色、class 名、h1 标签、字体粗细

### 共性 3：正文以 ■ 结尾（U+25A0）

- ■ 是固定 unicode U+25A0，不是 CSS 上色——直接 `in el.get_text()` 命中
- 三版式 6 本都验证存在（覆盖率约 70-75/76）
- ■ 总是出现在正文最后一段的末尾字符（不是单独段）
- ■ 是经济学人编辑惯例：标记"正文到此结束，后面是推广/分隔"

### 共性 4：■ 之后到下一篇文章之间是广告/水印/分隔（不进数据库）

- 部分版本有订阅广告文字（"Sign up to..."/"For subscribers..."/"Subscribers to The Economist..."）
- 部分版本有来源水印（"This article was downloaded by [calibre/zlibrary] from..."）
- 部分版本只有 hr/br 分隔符（阅读器可能渲染为广告位，但 HTML 无文字）
- **处理**：■ 截断即可全部清除（见 §6 步骤 5）

## 6. 提取流程（v3）

```
1. ebooklib 读 TOC → [(版块名, [(文章标题, 文章href), ...])]
2. 每个 TOC 条目 = 一篇文章起点
3. 文章范围 = 当前 href → 下一 href（跨文件自动拼接，B 版式文章跨 split）
4. 在范围内按位置顺序提取字段（不靠颜色/class）:
   位置1 短文本      → column_label
   位置2 长文本      → title（h1 或 p 都接受）
   位置3 中等文本    → subtitle（可选，无则 NULL）
   位置4 日期正则    → published_at + location
   位置5 含img块     → lead 题图
   位置6+ 段落       → body_blocks + body_text
5. ■ 截断（保证广告不进数据库）:
   找 ■ → 在 ■ 处截断正文（保留含■段，剥■字符；之后全丢）
   无■ → 不截断（少数文章，靠 TOC 边界自然隔离到下一文章）
6. 基本清洗（§8）+ 入库
7. 跳过零正文段切片（经济数据页）
```

**关键**：■ 是**清洗信号**不是边界信号。边界永远来自 TOC。■ 在 TOC 范围内用于截断广告/水印/分隔。

## 7. 日期正则（支持多 locale）

三格式：
- 英文：`Jul 09, 2026 05:22 AM | Singapore`
- 中文：`6月 18, 2026 03:17 上午 | BEIJING`
- 序数英文：`June 11th 2026`（无时间，published_at 留日期即可）

未匹配 → published_at=NULL + warn（不阻塞）。

文件名日期正则兼容 `-` 和 `.`：`(\d{4})[-.](\d{2})[-.](\d{2})`。

## 8. 清洗规则（基本卫生，入库前完成）

1. 去 navbar（导航条不采集）
2. 首字下沉无空格合并（`<b>Y</b><span>ou</span>` → You）
3. ■ 截断（§6 步骤 5，剥■字符 + 丢弃之后内容）
4. 剥除来源水印：`This article was downloaded by [calibre/zlibrary] from...`
5. 剥除文末推广语：`Dig deeper…` / `You can see previous ones here.`
6. 交叉引用链接：留文字弃 URL，`·` 分隔符转空格
7. 空白归一化（段内压缩、首尾 trim）
8. Unicode NFC（弯引号/破折号保留真实字符）

## 9. 数据库设计（schema.sql 已就绪，5 表）

```
issues    id PK, title, pub_date, source_file, article_count, created_at
sections  id PK, issue_id FK, name, sort_order
articles  id PK, issue_id FK, section_id FK, sort_order,
          column_label,          -- 文内栏目标签（红字/位置1）
          title,                 -- 网络版主标题
          subtitle NULL,         -- 副标题
          teaser NULL,           -- 索引页导语（" :: " 后半截；无则 NULL）
          published_at NULL,     -- TIMESTAMPTZ
          location NULL,         -- 日期行 | 后的地点
          body_blocks jsonb,     -- 行内级结构化正文（§10）
          body_text,             -- 清洗后纯文本（段落 \n\n 分隔）
          word_count,            -- 派生英文词数
          content_type           -- article / cartoon / brief
headings  id PK, article_id FK, text, sort_order
images    id PK, article_id FK, sort_order, storage_path, caption NULL,
          role                   -- lead=题图 / figure=内文图
```

约束：IDENTITY 主键、外键 CASCADE、RLS 全开无 policy（公开全拒，service_role/MCP 绕过）、私有 bucket `article-images`。图片上传 Storage，表内存路径。

图注双来源取非空：`img.title` ∪ 图下 div 文本。

## 10. body_blocks 行内级 schema（承重字段）

```json
body_blocks = [
  { "type": "p", "dropcap": true,  "runs": [<run>, ...] },
  { "type": "h",                    "runs": [<run>, ...] },
  { "type": "img", "ref": <images.sort_order> }
]
```

**run 类型**（行内，p/h 内的有序片段）：

| t | 含义 | 来源 |
|---|---|---|
| `text` | 纯文本 | 默认 |
| `b` | 粗体 | `<b>`/`<strong>` 或 CSS font-weight:bold |
| `i` | 斜体 | `<i>`/`<em>` 或 CSS font-style:italic |
| `sc` | 小型大写 | 经济学人标志性排版（大写+CSS） |
| `a` | 超链接 | 交叉引用，带 `href` |

**块类型**：`p`（段落，可带 `dropcap` 标记首字下沉）、`h`（小标题）、`img`（图，引用 images 表 sort_order）。

**消费端设计**：
- 网站/APP：写一个 run 渲染器（switch 函数，b→strong/i→em/sc→span/a→a，所有文章复用一个组件）
- 播客/TTS：直接用 body_text，不碰 runs
- body_blocks 是可移植中间表示（IR）：加新消费端 = 写新渲染器，数据不动

## 11. 显式假设清单（违反时大声报错，不静默产出垃圾）

- H1. EPUB 合法（ebooklib 能读 + 有 toc）—— 否则 abort
- H2. TOC 树至少 1 个版块含文章 href —— 否则 abort + 提示"TOC 无文章链接"
- H3. 文章 href 指向的文件存在于 zip —— 否则 skip + log
- H4. 文章第一页能按位置提取到标题（位置2）—— 否则 skip + log
- H5. 日期行匹配既有正则之一 —— 未匹配则 published_at=NULL + warn（不阻塞）
- H6. TOC 缺失则 warn，section="Unknown"，不阻塞

## 12. 实施顺序

1. 重写 ingest.py 为 v3（TOC 定位 + 位置顺序提取 + ■ 截断），保留清洗/schema/上传逻辑
2. 6 本 epub 本地验证（--no-upload）：篇数对齐 TOC、字段齐全、广告清零
3. drop 重建 DB（schema 已就绪）+ 重灌 6 期
4. git 收尾

用法：`python ingest.py <epub>` 零落盘幂等（同 pub_date 重灌先删后插）。
