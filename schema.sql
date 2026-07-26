-- schema.sql — readbay-article-database（经济学人结构化库）
-- ============================================================================
-- 项目: readbay-article-database (Supabase, ref qygqpedoqfctcopojcyt)
-- 用途: 经济学人 EPUB 逐篇提取为结构化数据，供个人网站/APP/播客复用
-- 设计依据: PLAN-v3.md（2026-07-26 定稿）
-- 文档: 见同目录 DATABASE-DOC.md（数据库外文档，与本文件 COMMENT ON 保持一致）
-- 执行方式: 由 agent 通过 Supabase MCP apply_migration 执行；或 Dashboard SQL Editor 粘贴运行
-- 安全: 5 表全部启用 RLS 且不建任何 policy —— anon/authenticated 默认全拒;
--        service_role(loader) 与 postgres(MCP) 绕过 RLS 正常读写
--        bucket 'article-images' 设为私有 (public=false)
-- 幂等: 开头 DROP ... CASCADE，每次跑 = 整库推倒重建（用户授权无兼容包袱）
-- 字段类型说明: text/jsonb 在 PostgreSQL 中无长度限制（最大约 1GB），
--              文章正文/body_blocks 永远不会因"太长"存不下
-- ============================================================================

begin;

-- ============================================================ 清空旧结构（含数据 + 表结构 + 约束 + 索引）

drop table if exists images    cascade;
drop table if exists headings  cascade;
drop table if exists articles  cascade;
drop table if exists sections  cascade;
drop table if exists issues    cascade;

-- ============================================================ 表结构

-- 期刊表 | Issues — one row per Economist EPUB issue (weekly)
create table issues (
    id            bigint generated always as identity primary key,
    title         text not null,
    pub_date      date not null,
    source_file   text not null,
    article_count integer not null default 0,
    created_at    timestamptz not null default now()
);

-- 版块表 | Sections — one row per TOC section (Leaders, Britain, United States, etc.)
create table sections (
    id         bigint generated always as identity primary key,
    issue_id   bigint not null references issues(id) on delete cascade,
    name       text not null,
    sort_order integer not null
);

-- 文章主表 | Articles — main table, one row per article
create table articles (
    id           bigint generated always as identity primary key,
    issue_id     bigint not null references issues(id) on delete cascade,
    section_id   bigint not null references sections(id) on delete cascade,
    sort_order   integer not null,
    column_label text,
    title        text not null,
    subtitle     text,
    teaser       text,
    published_at timestamptz,
    location     text,
    body_blocks  jsonb not null,
    body_text    text not null,
    word_count   integer not null default 0,
    content_type text not null default 'article'
);

-- 小标题表 | Headings — article subheadings (h2-h6 in source EPUB)
create table headings (
    id         bigint generated always as identity primary key,
    article_id bigint not null references articles(id) on delete cascade,
    text       text not null,
    sort_order integer not null
);

-- 图片表 | Images — article images, binary stored in private Storage bucket 'article-images'
create table images (
    id           bigint generated always as identity primary key,
    article_id   bigint not null references articles(id) on delete cascade,
    sort_order   integer not null,
    storage_path text not null,
    caption      text,
    role         text not null default 'figure'
);

-- ============================================================ 索引

create index sections_issue_id_idx   on sections(issue_id);
create index articles_issue_id_idx   on articles(issue_id);
create index articles_section_id_idx on articles(section_id);
create index articles_published_idx  on articles(published_at);
create index headings_article_id_idx on headings(article_id);
create index images_article_id_idx   on images(article_id);
create index images_role_idx         on images(article_id, role);

-- ============================================================ 行级安全 (RLS)
-- 只启用、不建 policy = 对 anon/authenticated 默认全拒
-- service_role(loader) 与 postgres(MCP) 不受 RLS 约束，正常读写

alter table issues    enable row level security;
alter table sections  enable row level security;
alter table articles  enable row level security;
alter table headings  enable row level security;
alter table images    enable row level security;

-- ============================================================ 私有图片 bucket

insert into storage.buckets (id, name, public)
values ('article-images', 'article-images', false)
on conflict (id) do nothing;

-- ============================================================ 注释（中英双语，Supabase Dashboard 可见）
-- 交接时: 打开 Supabase Dashboard → Table Editor → 每个表/列的描述字段即此处 COMMENT
-- 完整文档见同目录 DATABASE-DOC.md

-- ---------- issues 表注释
comment on table  issues is '期刊表 | Issues — one row per Economist EPUB issue (weekly). 顶层表，每期经济学人 EPUB 对应一行。幂等重灌: 同 pub_date 旧刊先 delete(外键级联) 再 insert。';
comment on column issues.id is '主键 | PK — bigint GENERATED ALWAYS AS IDENTITY (Supabase 推荐写法，非 uuid)';
comment on column issues.title is '期刊标题 | Issue title — 格式 "The Economist YYYY-MM-DD"，由文件名日期拼接';
comment on column issues.pub_date is '出版日期 | Publication date — date 类型，从 EPUB 文件名解析 (兼容 - 和 . 分隔)；同 pub_date 重灌时作幂等删旧依据';
comment on column issues.source_file is '源 EPUB 文件名 | Source EPUB filename — 如 "TE-2026-07-11-EPUB.epub"，溯源用';
comment on column issues.article_count is '本期文章数 | Article count — 入库的文章总数 (经济数据页等零正文切片已 skip，不计入)';
comment on column issues.created_at is '记录创建时间 | Record creation timestamp — timestamptz, 默认 now()';

-- ---------- sections 表注释
comment on table  sections is '版块表 | Sections — one row per TOC section (Leaders, Britain, United States, Finance, etc.). 版块来自 EPUB 的 TOC (ebooklib book.toc)，每期约 19-21 个版块。';
comment on column sections.id is '主键 | PK — bigint identity';
comment on column sections.issue_id is '所属期刊外键 | FK to issues.id — ON DELETE CASCADE，删期刊级联删版块';
comment on column sections.name is '版块名 | Section name — 来自 TOC，如 "Leaders"/"Britain"/"United States"/"The world this week"/"Finance & economics"';
comment on column sections.sort_order is '版块在期刊内的顺序 | Sort order within issue — 从 0 开始，按 TOC 顺序';

-- ---------- articles 表注释
comment on table  articles is '文章主表 | Articles — main table, one row per article. 经济学人每期约 70-80 篇。body_blocks=渲染用 IR (行内级 runs)，body_text=检索/TTS 用纯文本。content_type: article/cartoon。';
comment on column articles.id is '主键 | PK — bigint identity';
comment on column articles.issue_id is '所属期刊外键 | FK to issues.id — ON DELETE CASCADE';
comment on column articles.section_id is '所属版块外键 | FK to sections.id — ON DELETE CASCADE';
comment on column articles.sort_order is '文章在版块内的顺序 | Sort order within section — 从 0 开始';
comment on column articles.column_label is '栏目标签 | Column label — 文章开头红字短文本 (位置1)。专栏文章是专栏名 (Bartleby/Bagehot/Charlemagne/Free Exchange/Buttonwood 等)；"The world this week" 版块为版块名；少数文章无 -> NULL';
comment on column articles.title is '主标题 | Main title — 网络版主标题 (铁律: 一律以 EPUB 网络版为准，不用印刷版)。位置2，h1 或 p 标签都接受';
comment on column articles.subtitle is '副标题 | Subtitle — 位置3，少数文章无 -> NULL (如 The world this week 的 Politics/Business 简讯)';
comment on column articles.teaser is '索引页导语 | Index page teaser — TOC 索引页一句话点题 (含 " :: " 时取后半截)。v3 暂未提取，全 NULL，字段预留供未来补';
comment on column articles.published_at is '发布时间 | Published datetime — timestamptz，由文章内日期行正则解析。三格式: 英文 "Jul 09, 2026 05:22 AM"、中文 "6月 18, 2026 03:17 上午"、序数英文 "June 11th 2026"。源站未给时区按 UTC (+00:00)。少数文章无日期行 -> NULL';
comment on column articles.location is '地点 | Location — 日期行 "|" 后的地点 (如 Singapore/BEIJING)，可缺 -> NULL。约 33% 文章有';
comment on column articles.body_blocks is '行内级结构化正文 IR | Inline-level structured body IR — jsonb，承重字段，支撑网站/APP 按原排版 1:1 还原。格式: [{type:"p",runs:[{t:"text"|"b"|"i"|"sc"|"a",x:"...",href?:"..."},...],dropcap?:true} | {type:"h",runs:[...]} | {type:"img",ref:<images.sort_order>}]。t: text/b=粗体/i=斜体/sc=小型大写/a=超链接。dropcap: 首字下沉标记。ref: 引用同文章内 images.sort_order。消费端写 run 渲染器 (b->strong/i->em/sc->span/a->a)。详见 DATABASE-DOC.md';
comment on column articles.body_text is '清洗后纯文本 | Cleaned plain text — 段落用 \n\n 分隔，管全文搜索/播客 TTS。已执行清洗: 去navbar/首字下沉合并/■截断/剥水印/剥推广语/交叉引用留文字弃URL/空白归一/NFC';
comment on column articles.word_count is '英文词数 | English word count — body_text 的英文词数 (正则 [A-Za-z0-9''-]+)。中文文章词数不准，但经济学人是英文刊物不影响';
comment on column articles.content_type is '内容类型 | Content type — text，默认 "article"。当前值: article (普通文章) / cartoon (漫画，靠 title 以 "cartoon" 开头识别)。无 CHECK 约束，未来可扩展 brief/data';

-- ---------- headings 表注释
comment on table  headings is '小标题表 | Headings — article subheadings (源 EPUB 中的 h2-h6 标签)。部分期 (如 07-04 经济学人原生 EPUB) 文章无 h 标签 -> 该期 headings 为 0，属正常。';
comment on column headings.id is '主键 | PK — bigint identity';
comment on column headings.article_id is '所属文章外键 | FK to articles.id — ON DELETE CASCADE';
comment on column headings.text is '小标题文本 | Heading text — 已清洗';
comment on column headings.sort_order is '小标题在文章内的顺序 | Sort order within article — 从 0 开始，按文档序';

-- ---------- images 表注释
comment on table  images is '图片表 | Images — article images. 图片二进制存私有 Storage bucket "article-images"，表内存 storage_path。每期约 100-140 张。';
comment on column images.id is '主键 | PK — bigint identity';
comment on column images.article_id is '所属文章外键 | FK to articles.id — ON DELETE CASCADE';
comment on column images.sort_order is '图片在文章内的顺序 | Sort order within article — 从 0 开始；lead 题图先编号，figure 内文图按文档序';
comment on column images.storage_path is 'Storage 路径 | Storage path — 格式 "{pub_date}/{article_key}_{sort_order+1}{ext}"，如 "2026-07-11/art_5_1.jpg"。私有 bucket，访问需签 URL';
comment on column images.caption is '图注 | Caption — 双来源取非空: img.title ∪ img.alt ∪ 图下 div/p 文本。无则 NULL (铁律: 每张图只携带自己的图注，绝不串借)。约 30-40% 图片无图注';
comment on column images.role is '图片角色 | Image role — text，默认 "figure"。lead=题图 (头部区块内的 img，在正文开始前) / figure=内文图 (正文中)。body_blocks 的 {type:"img",ref} 通过此 sort_order 关联';

commit;
