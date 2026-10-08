"""Rebuild the offline architecture atlas using Python's standard library only."""
from pathlib import Path
from html import escape as e
import ast
import hashlib
import json
from datetime import datetime
from content import GROUPS, NODES, TERMS

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
BY_ID = {n['id']: n for n in NODES}
TERM_MAP = {t[0]: t for t in TERMS}
SOURCES = {}
STAMP = datetime.now().strftime('%Y-%m-%d %H:%M')
for group, number, title, en, summary, ids in GROUPS:
    for pos, ident in enumerate(ids, 1):
        BY_ID[ident].update(group=group, number=f'{number}.{pos}', group_title=title)

def code_slug(path):
    return path.replace('/', '--') + '.html'

def read_source(path):
    if path not in SOURCES:
        source = (PROJECT / path).read_text(encoding='utf-8-sig')
        symbols = {}
        tree = ast.parse(source)
        def visit(body, prefix=''):
            for item in body:
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + item.name
                    symbols[name] = (item.lineno, item.end_lineno)
                    visit(item.body, name + '.')
        visit(tree.body)
        SOURCES[path] = (source, symbols)
    return SOURCES[path]

def resolve(path, symbol):
    _, symbols = read_source(path)
    if symbol not in symbols:
        raise ValueError(f'Missing code entry: {path}::{symbol}')
    return symbols[symbol]

def source_href(path, symbol=None, prefix=''):
    start = resolve(path, symbol)[0] if symbol else 1
    if not symbol:
        read_source(path)
    return f'{prefix}code/{code_slug(path)}#L{start}'

def terms_table(ids):
    rows = ''.join(f'<tr><th scope="row"><a href="../glossary.html#term-{i}">{e(TERM_MAP[i][1])}</a></th><td lang="en">{e(TERM_MAP[i][2])}</td><td>{e(TERM_MAP[i][3])}</td></tr>' for i in ids)
    return f'<div class="table-scroll"><table><thead><tr><th>中文 / 缩写</th><th>英文全名</th><th>在项目中的含义</th></tr></thead><tbody>{rows}</tbody></table></div>'

def shell(title, content, depth=0, active='overview'):
    prefix = '../' * depth
    nav = ''
    for group, number, name, en, summary, ids in GROUPS:
        nav += f'<div class="nav-group"><p>{number} / {name}</p>'
        for ident in ids:
            n = BY_ID[ident]
            selected = ' aria-current="page"' if active == ident else ''
            nav += f'<a class="nav-node {"active" if active == ident else ""}" href="{prefix}nodes/{ident}.html"{selected}><span class="nav-dot {group}"></span>{n["title"]}</a>'
        nav += '</div>'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta name="description" content="KnowledgeOps Copilot 的真实代码架构、流程、源码入口与中英术语导航。"><title>{e(title)} · KnowledgeOps Atlas</title><link rel="stylesheet" href="{prefix}assets/style.css"></head>
<body data-base="{prefix}"><a class="skip" href="#main">跳至正文</a>
<aside class="sidebar" id="sidebar"><a class="brand" href="{prefix}index.html"><span class="brand-icon">K<span>↗</span></span><span>KnowledgeOps<small>ARCHITECTURE ATLAS</small></span></a>
<div class="sidebar-label">项目架构导航 <span>v1.0</span></div><nav aria-label="项目导航"><a class="overview-link {"active" if active == "overview" else ""}" href="{prefix}index.html">◈ <span>整体架构</span><span>↗</span></a>{nav}<a class="utility {"active" if active == "glossary" else ""}" href="{prefix}glossary.html">Aa <span>中英术语表</span><span>{len(TERMS)}</span></a><a class="utility {"active" if active == "code" else ""}" href="{prefix}code-index.html">⌘ <span>源码入口索引</span><span>↗</span></a></nav>
<div class="sidebar-foot"><span class="live-dot"></span> 本地静态文档<small>基于当前代码与设计取舍<br>核对日期 · 2026.10.08</small></div></aside>
<div class="workspace"><header class="topbar"><div class="top-label"><button class="menu-button" aria-label="打开导航" aria-expanded="false" aria-controls="sidebar">☰</button><span>ENGINEERING FIELD GUIDE</span><i>/</i><span>代码与流程</span></div><button class="search-trigger" type="button"><span>⌕</span> 搜索节点、术语或函数 <kbd>/</kbd></button></header>
<main id="main" tabindex="-1">{content}</main><footer class="footer"><span>KnowledgeOps Copilot · 从系统全貌，到每一行实现。</span><a href="{prefix}index.html">回到整体架构 ↑</a></footer></div>
<dialog id="search-dialog" aria-labelledby="search-label"><form method="dialog" class="search-head"><label id="search-label" for="global-search">搜索架构导航</label><button aria-label="关闭搜索">Esc ×</button></form><input id="global-search" type="search" placeholder="试试：审批、RRF、ChatService、SSE…" autocomplete="off"><div id="search-results" aria-live="polite"></div></dialog>
<script src="{prefix}assets/search-data.js"></script><script src="{prefix}assets/app.js"></script></body></html>'''

def write(path, content):
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')

def node_card(ident, compact=False):
    n = BY_ID[ident]
    return f'<a class="diagram-node {n["group"]}" href="nodes/{ident}.html"><div class="node-top"><span>{n["number"]}</span><span>↗</span></div><h3>{n["title"]}</h3><p lang="en">{n["en"]}</p><small>{n["day"]}</small></a>'

def build_home():
    lanes = ''
    notes = {
        'ingest':'上传异步解析 · 切分与索引另行触发',
        'rag':'POST /chat · 检索 → 上下文 → 生成 → 引用',
        'agent':'POST /agent · 循环内调用工具，写操作进入审批',
    }
    for group, number, title, en, summary, ids in GROUPS[:3]:
        lanes += f'<section class="lane" data-lane="{group}"><div class="lane-heading"><h3><span class="lane-number {group}">{number}</span>{title}</h3><span>{notes[group]}</span></div><div class="lane-track">' + ''.join(node_card(i) for i in ids) + '</div></section>'
    foundations = ''.join(node_card(i, True) for i in GROUPS[3][5])
    comparisons = [
        ('数据持久化','统一 PostgreSQL + pgvector','同一 PostgreSQL：文档 / 会话 / 向量 / Agent'),
        ('入库调度','上传到索引的完整管道','worker 完成解析清洗；切分、索引单独调用'),
        ('文档更新','新版本索引就绪后再切换','先删除旧切分头，再重建；有暂时不可检索窗口'),
        ('检索策略','混合检索与可选重排','冻结配置 hybrid；重排关闭；无环境配置回退 vector'),
        ('任务恢复','持久化与恢复能力','显式 /resume；没有后台扫描积压 Agent 任务'),
        ('部署交付','本地容器与 CI','本地应用 Dockerfile / Compose 已配置；CI 待远端首次运行'),
    ]
    comparison_rows = ''.join(f'<tr><th scope="row">{a}</th><td>{b}</td><td>{c}</td></tr>' for a,b,c in comparisons)
    content = f'''<section class="hero"><div><div class="eyebrow"><span></span> SYSTEM OVERVIEW / 系统总览</div><h1>从一份文档，<br>到一个<span>可信的行动。</span></h1><p>KnowledgeOps Copilot 的架构地图。沿着三条主线，<br class="desktop">理解证据如何流动、模型如何决策，以及代码如何守住边界。</p></div><div class="hero-aside"><div class="orbital" aria-hidden="true"><span>K</span><i class="orbit-dot one"></i><i class="orbit-dot two"></i><i class="orbit-dot three"></i></div><div class="hero-numbers"><div><strong>03</strong><span>核心流程</span></div><div><strong>16</strong><span>可探索节点</span></div><div><strong>{len(TERMS)}</strong><span>中英术语</span></div></div></div></section>
<section class="architecture" aria-labelledby="architecture-title"><div class="section-header"><div><div class="eyebrow">THE SYSTEM MAP</div><h2 id="architecture-title">整体架构<span>点击任一节点，进入实现细节</span></h2></div><div class="map-filters" role="group" aria-label="聚焦流程"><button class="selected" data-focus="all" aria-pressed="true">全部</button><button data-focus="ingest" aria-pressed="false">入库</button><button data-focus="rag" aria-pressed="false">问答</button><button data-focus="agent" aria-pressed="false">Agent</button></div></div>
<div class="map-entry"><span class="entry-client">用户 / API 客户端</span><span class="entry-arrow">→</span><a href="nodes/api.html"><b>FastAPI</b><span>请求 ID · 身份注入 · 依赖装配</span><span>↗</span></a><span class="entry-branch">↓ 分流到业务链路</span></div>
<div class="lanes">{lanes}</div><div class="cross-links"><span>↳ 入库向量 → 问答召回</span><span>↳ Agent 知识工具 → 复用检索服务</span><span>↳ 工单写入 → 审批事务 → 继续循环</span></div>
<div class="foundation-heading"><span>贯穿三条主线的工程支撑</span><span>FOUNDATIONS</span></div><div class="foundation-track">{foundations}</div><div class="storage-rail"><a href="nodes/storage.html"><b>PostgreSQL · 元数据</b><span>文档 · 切分 · 权限 · 会话</span></a><span>当前集合 ID 关联 · 分阶段发布 · 当前集合校验</span><a href="nodes/storage.html"><b>PostgreSQL + pgvector</b><span>向量 · Agent 检查点 · 模拟工单</span></a></div>
<p class="map-caption"><span class="live-dot"></span> 图中节点均有当前代码入口。箭头表示逻辑流向；Agent 包含循环与条件分支，详见节点页。重排为可选能力。</p></section>
<section class="reading-section"><div class="section-header"><div><div class="eyebrow">CHOOSE A PATH</div><h2>从你的问题出发</h2></div></div><div class="reading-grid"><a href="nodes/upload.html"><span>01 / 数据怎样准备好？</span><h3>把文档变成证据 <b>↗</b></h3><p>上传 → 解析 → 切分 → 索引</p></a><a href="nodes/generate.html"><span>02 / 回答为什么可信？</span><h3>追到一条结论的来源 <b>↗</b></h3><p>授权召回 → 上下文 → Claim → 引用</p></a><a href="nodes/approval.html"><span>03 / 工具会不会乱执行？</span><h3>看懂一次工单提交 <b>↗</b></h3><p>模型提议 → 人工审批 → 幂等事务</p></a></div></section>
<section class="reality-section"><div class="section-header"><div><div class="eyebrow">DESIGN ↔ IMPLEMENTATION</div><h2>目标架构与当前实现</h2></div><a class="text-link" href="../design.md">查看架构与设计取舍 ↗</a></div><p class="section-intro">以当前代码和可复现流程为依据，区分设计目标、已实现功能与验证边界。</p><div class="table-scroll"><table><thead><tr><th>关注点</th><th>设计目标</th><th>当前代码与配置</th></tr></thead><tbody>{comparison_rows}</tbody></table></div><div class="notice"><strong>已实现 ≠ 已全面验证</strong><p>历史保留集有真实模型记录，但语义完整性仍有已知缺口；Agent 工程场景主要使用脚本策略。真实 TTFT、云并发、修复后的新批准工单措辞仍待验证。<a href="nodes/evaluation.html">查看评测口径 →</a></p></div></section>
<section class="journey"><div><div class="eyebrow">PROJECT WALKTHROUGH</div><h2>沿着业务流程阅读代码</h2></div><div class="journey-track"><a href="nodes/api.html"><b>01</b><span>身份与接口</span></a><a href="nodes/upload.html"><b>02</b><span>数据与索引</span></a><a href="nodes/generate.html"><b>03</b><span>检索与问答</span></a><a href="nodes/loop.html"><b>04</b><span>Agent 与可靠性</span></a><a href="nodes/operations.html"><b>05</b><span>运行与验证</span></a></div></section>'''
    write('index.html',shell('整体架构',content))

def build_detail(n):
    refs = ''
    for path,symbol,role in n['refs']:
        start,end = resolve(path,symbol)
        source = read_source(path)[0].splitlines()
        excerpt = '\n'.join(f'{i+1:>4}  {source[i]}' for i in range(start-1,min(end,start+7)))
        refs += f'<article class="code-entry"><div><span class="code-role">{e(role)}</span><a href="{source_href(path,symbol,"../")}">{e(symbol)} <span>↗</span></a><p>{e(path)}<span> L{start}–{end}</span></p></div><details><summary>预览入口代码</summary><pre><code>{e(excerpt)}</code></pre></details></article>'
    steps = ''.join(f'<li><span class="step-num">{i:02}</span><div><h3>{e(title)}</h3><p>{e(body)}</p></div></li>' for i,(title,body) in enumerate(n['steps'],1))
    bounds = ''.join(f'<article><h3>{e(title)}</h3><p>{e(body)}</p></article>' for title,body in n['boundary'])
    tests = ''.join(f'<a class="test-link" href="{source_href(p,prefix="../")}"><span>↗</span><code>{e(p)}</code></a>' for p in n['tests'])
    related = ''.join(f'<a href="{i}.html"><span>{BY_ID[i]["number"]}</span><b>{BY_ID[i]["title"]}</b><span>→</span></a>' for i in n['related'])
    group = next(g for g in GROUPS if g[0]==n['group'])
    rail = ''.join(f'<a href="{i}.html" {"aria-current=page" if i==n["id"] else ""}><span>{BY_ID[i]["number"]}</span>{BY_ID[i]["title"]}</a>' for i in group[5])
    content = f'''<div class="breadcrumb"><a href="../index.html">整体架构</a><span>/</span>{n['group_title']}<span>/</span>{n['title']}</div>
<header class="detail-hero {n['group']}"><div class="eyebrow">NODE {n['number']} / {n['day']}</div><h1>{n['title']}</h1><p class="english-title" lang="en">{n['en']}</p><p class="detail-summary">{n['summary']}</p><div class="status"><span class="live-dot"></span> 当前代码已实现 <span>·</span> 具体配置与验证边界见下文</div></header>
<nav class="flow-rail" aria-label="所在流程">{rail}</nav><div class="detail-layout"><div class="detail-body">
<section id="contract"><div class="section-kicker">01 / RESPONSIBILITY</div><h2>这个节点接收什么，交付什么？</h2><div class="io-grid"><article><span>INPUT / 输入</span><p>{e(n['inputs'])}</p></article><article><span>OUTPUT / 输出</span><p>{e(n['outputs'])}</p></article></div></section>
<section id="flow"><div class="section-kicker">02 / EXECUTION FLOW</div><h2>代码是怎样一步步执行的？</h2><ol class="steps">{steps}</ol><div class="example"><div>{e(n['example'][0])}<button type="button" class="copy-button">复制</button></div><pre><code>{e(n['example'][1])}</code></pre></div></section>
<section id="code"><div class="section-kicker">03 / CODE ENTRIES</div><h2>从这里进入源码</h2><p class="section-intro">按调用职责排列。点击函数名，在本站源码阅读页定位真实行号；展开可快速预览。</p><div class="code-entries">{refs}</div></section>
<section id="boundaries"><div class="section-kicker">04 / DESIGN BOUNDARIES</div><h2>需要讲清的设计边界</h2><div class="boundaries">{bounds}</div></section>
<section id="terms"><div class="section-kicker">05 / TERMINOLOGY</div><h2>专业术语 · 中英全名</h2>{terms_table(n['terms'])}</section>
<section id="evidence"><div class="section-kicker">06 / VERIFICATION</div><h2>从测试与记录验证理解</h2><p class="section-intro">以下为现有测试源码入口，帮助理解覆盖场景。本导航未重新运行后端测试或调用真实模型。</p>{tests}<a class="text-link" href="../../verification.md">项目评测说明 ↗</a></section>
<section class="related"><div class="section-kicker">KEEP EXPLORING</div><h2>继续沿着数据流走</h2><div>{related}</div></section></div>
<aside class="page-toc"><span>本页导航</span><a href="#contract">01 输入与输出</a><a href="#flow">02 执行流程</a><a href="#code">03 代码入口</a><a href="#boundaries">04 设计边界</a><a href="#terms">05 中英术语</a><a href="#evidence">06 测试证据</a><div class="toc-note">阅读建议<p>先跟一条成功路径，再看失败分支与持久化边界。</p></div></aside></div>'''
    write(f'nodes/{n["id"]}.html',shell(n['title'],content,1,n['id']))

def build_glossary():
    cards = ''.join(f'<article id="term-{i}" class="term-card" data-search="{e((name+en+desc).lower(),quote=True)}"><span>{idx:02}</span><div><h2>{e(name)}</h2><p lang="en">{e(en)}</p><div>{e(desc)}</div></div><a href="#term-{i}" aria-label="{e(name)}的链接">#</a></article>' for idx,(i,name,en,desc) in enumerate(TERMS,1))
    content = f'''<div class="breadcrumb"><a href="index.html">整体架构</a><span>/</span>中英术语表</div><header class="page-hero"><div class="eyebrow">THE SHARED VOCABULARY</div><h1>把专业名词，<br><span>放回代码语境。</span></h1><p>{len(TERMS)} 个术语 · 中文含义 / 英文全名 / 项目用法</p></header><div class="local-filter"><label for="term-filter">筛选术语</label><input id="term-filter" type="search" placeholder="输入缩写、英文或中文，例如：RRF、审批、词元"><span id="term-count" aria-live="polite">{len(TERMS)} 项</span></div><div class="term-list">{cards}</div><p id="term-empty" hidden>没有匹配的术语，试试其他关键词。</p>'''
    write('glossary.html',shell('中英术语表',content,active='glossary'))

def build_code():
    rows = ''
    for n in NODES:
        for path,symbol,role in n['refs']:
            line,_ = resolve(path,symbol)
            rows += f'<tr data-search="{e((path+symbol+role+n["title"]).lower(),quote=True)}"><td><a href="nodes/{n["id"]}.html">{n["number"]} {n["title"]}</a></td><td><a href="{source_href(path,symbol)}"><b>{e(symbol)}</b></a><small>{e(path)}:{line}</small></td><td>{e(role)}</td></tr>'
    content = f'''<div class="breadcrumb"><a href="index.html">整体架构</a><span>/</span>源码入口索引</div><header class="page-hero"><div class="eyebrow">FROM CONCEPT TO CODE</div><h1>每个节点，<br><span>都有真实入口。</span></h1><p>函数名与行号从 Python 语法树提取并校验；点击即可阅读对应源码快照。</p></header><div class="local-filter"><label for="code-filter">筛选入口</label><input id="code-filter" type="search" placeholder="搜索模块、函数或职责"><span id="code-count" aria-live="polite"></span></div><div class="table-scroll"><table class="code-index"><thead><tr><th>所属节点</th><th>函数 / 文件位置</th><th>职责</th></tr></thead><tbody>{rows}</tbody></table></div><p id="code-empty" hidden>没有匹配的代码入口。</p><div class="notice"><strong>源码快照说明</strong><p>快照在生成导航时读取，只包含明确引用的项目 Python 文件和测试。更新项目后运行 build.py 同步行号与代码；文字解释需另行复核。未读取 .env、运行数据库或模型密钥。</p></div>'''
    write('code-index.html',shell('源码入口索引',content,active='code'))
    manifest = []
    for path,(source,symbols) in sorted(SOURCES.items()):
        digest = hashlib.sha256((PROJECT/path).read_bytes()).hexdigest()
        manifest.append(dict(path=path,sha256=digest,lines=len(source.splitlines())))
        lines = ''.join(f'<div class="source-line" id="L{i}"><a href="#L{i}" aria-label="第 {i} 行">{i}</a><code>{e(line) or " "}</code></div>' for i,line in enumerate(source.splitlines(),1))
        symbol_links = ''.join(f'<a href="#L{start}">{e(name)} <small>L{start}</small></a>' for name,(start,end) in symbols.items() if name.count('.')<=1)
        linked_nodes = [n for n in NODES if any(r[0]==path for r in n['refs']) or path in n['tests']]
        backlinks = ''.join(f'<a href="../nodes/{n["id"]}.html">{n["title"]} ↗</a>' for n in linked_nodes)
        content = f'''<div class="breadcrumb"><a href="../index.html">整体架构</a><span>/</span><a href="../code-index.html">源码索引</a></div><header class="source-header"><div class="eyebrow">SOURCE SNAPSHOT / 源码快照</div><h1>{e(path)}</h1><p>{len(source.splitlines())} 行 · 生成于 {STAMP} · SHA-256 {digest[:12]}…</p><div class="source-actions"><a href="../../../{path}">打开原始文件 ↗</a><button type="button" class="back-button">← 返回上一页</button></div><div class="source-backlinks">{backlinks}</div></header><details class="symbol-list"><summary>定位函数与类（{len(symbols)}）</summary><div>{symbol_links}</div></details><div class="source-code" aria-label="带行号的 Python 源码">{lines}</div>'''
        write(f'code/{code_slug(path)}',shell(path,content,1,'code'))
    write('source-manifest.json',json.dumps(dict(generated_at=STAMP,files=manifest),ensure_ascii=False,indent=2)+'\n')

def build_search():
    entries = []
    for n in NODES:
        entries.append(dict(title=n['title'],label=f'{n["number"]} / {n["group_title"]}',description=n['summary'],url=f'nodes/{n["id"]}.html',keywords=' '.join([n['en'],n['day'],*[r[0]+' '+r[1]+' '+r[2] for r in n['refs']]])))
    for i,name,en,desc in TERMS:
        entries.append(dict(title=name,label='中英术语',description=en+' · '+desc,url=f'glossary.html#term-{i}',keywords=en))
    write('assets/search-data.js','window.ATLAS_SEARCH = '+json.dumps(entries,ensure_ascii=False)+';\n')

if __name__ == '__main__':
    build_home()
    for n in NODES:
        build_detail(n)
    build_glossary()
    build_code()
    build_search()
    print(f'Built 16 node pages, {len(TERMS)} terms, {len(SOURCES)} source snapshots.')
