"""搜索正文高亮（spec 2026-11-24 §4，T-FE2 lane）静态钉家族 + node 探针。

覆盖四面：
  ① bodyHighlight.js —— node 探针（导出形状/空词元短路/非法 root 防呆）+ 静态结构钉
     （DOM 行为 node 不可直跑，浏览器目检归主会话）；
  ② app.js 生命周期 —— state 初值/pick 落账字面 + 三处清零点字面 + 一次性令牌；
  ③ EditorPane 挂点顺序 —— postProcessPreview 与 writeSrcLineMapping 之间；另有跟落账
     双向同步的 DOM watch（联调返工 2026-11-24 收口：v-show 常驻 + 输入未变 = 零 patch，
     state 面变更必须配套 DOM 面——清零→解包旧 span，非 null 且同 path→补打
     「进编辑熄灭→再点同一条结果」场景）；
  ④ CSS 在场钉 —— **预期红**：style.css 归主会话（本 lane 禁改），.doc-hit 规则由
     主会话合流时落盘后此钉转绿；其余各钉合流前必须绿。
"""
from __future__ import annotations

import pathlib
import re

TPL = pathlib.Path(__file__).resolve().parent.parent / "server" / "templates"


def _js(rel: str) -> str:
    return (TPL / rel).read_text(encoding="utf-8")


def _run_node(js: str):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node missing")
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)


# ────────────────────────── ① bodyHighlight.js ──────────────────────────

def test_body_highlight_node_probe_shape():
    """node 探针：导出形状 + 空/非法词元短路 + 非法 root 防呆（零 DOM 依赖面）。"""
    bh = TPL / "js/utils/bodyHighlight.js"
    assert bh.exists(), "bodyHighlight.js 未建"
    mod = bh.as_uri()
    js = (
        "import(%r).then(m=>{"
        "const bad=(w)=>{console.error(w);process.exit(1)};"
        "if(typeof m.highlightDocTerms!=='function')bad('highlightDocTerms 非函数');"
        "if(typeof m.clearDocHits!=='function')bad('clearDocHits 非函数');"
        # 空词元短路：root 同为非法（{}/null）仍须返回清除函数、零抛——短路先于 root 访问
        "if(typeof m.highlightDocTerms({},[])!=='function')bad('空词元未返回清除函数');"
        "if(typeof m.highlightDocTerms(null,undefined)!=='function')bad('undefined 词元未返回清除函数');"
        "if(typeof m.highlightDocTerms({},'x')!=='function')bad('非数组词元未返回清除函数');"
        "if(typeof m.highlightDocTerms({},[0,null,''])!=='function')bad('全非法词元未返回清除函数');"
        # 词元非空但 root 非法（node 无 document）：防呆返回清除函数，不触碰 DOM
        "if(typeof m.highlightDocTerms({},['x'])!=='function')bad('非法 root 未防呆');"
        "if(typeof m.highlightDocTerms(null,['x'])!=='function')bad('null root 未防呆');"
        # clearDocHits 非法输入防呆
        "m.clearDocHits(null);m.clearDocHits({});"
        # fake-DOM 解包行为（联调返工 2026-11-24 清除面承重）：子节点先上浮、壳后移除、
        # normalize 被调——顺序颠倒（先删壳）会静默毁正文，本探针即防此回归
        "const mk=(t,x)=>({type:t,text:x,children:[],parentNode:null,"
        "get firstChild(){return this.children[0]||null},"
        "insertBefore(node,ref){const p=node.parentNode;if(p){p.children=p.children.filter(n=>n!==node)};const i=this.children.indexOf(ref);this.children.splice(i<0?this.children.length:i,0,node);node.parentNode=this},"
        "removeChild(n){this.children=this.children.filter(c=>c!==n)}});"
        "const hx=mk('text','hit');"
        "const hs=mk('span',null);hs.children.push(hx);hx.parentNode=hs;"
        "const tx=mk('text','x'),ty=mk('text','y');"
        "const box=mk('root',null);box.children=[tx,hs,ty];hs.parentNode=box;tx.parentNode=box;ty.parentNode=box;"
        "let norm=0;box.normalize=()=>{norm++};"
        "box.querySelectorAll=()=>[hs];"
        "m.clearDocHits(box);"
        "const cs=box.children;"
        "if(cs.length!==3)bad('解包后子数应为3(壳除、命中文本上浮留位),got '+cs.length);"
        "if(cs[1]!==hx||hx.parentNode!==box)bad('命中文本未上浮或缺父挂');"
        "if(cs.some(c=>c.type==='span'))bad('壳未除');"
        "if(norm===0)bad('normalize 未调(相邻文本合并缺,textContent 兜底比对前提破)');"
        "}).catch(e=>{console.error(e);process.exit(1)})"
    ) % mod
    _run_node(js)


def test_body_highlight_static_shape():
    """静态结构钉：DOM 行为留浏览器目检，结构要素必须在场。"""
    bh = _js("js/utils/bodyHighlight.js")
    assert "createTreeWalker" in bh and "NodeFilter.SHOW_TEXT" in bh, "缺 TreeWalker 文本节点遍历"
    m = re.search(r"'(code,pre[^']*)'", bh)
    assert m, "缺免疫面选择器"
    sel = m.group(1)
    for token in ("code", "pre", ".mermaid", ".doc-hit"):
        assert token in sel, f"免疫面缺 {token}（code/pre/mermaid/既有 doc-hit 幂等防重包）"
    assert re.search(r"className\s*=\s*'doc-hit'", bh), "缺包 span 类名 doc-hit"
    assert "toLowerCase()" in bh, "缺词元小写归一"
    assert re.search(r"sort\(\s*\(a,\s*b\)\s*=>\s*b\.length\s*-\s*a\.length", bh), \
        "缺长词优先排序（alternation 短吞长防御）"
    assert "escapeRe" in bh, "缺正则元字符转义（词元注入防御）"
    assert "normalize()" in bh, "缺 clear 后 normalize（textContent 兜底比对逐字不变前提）"


# ────────────────────────── ② app.js 生命周期 ──────────────────────────

def test_app_state_shape_and_pick_assignment():
    """state 初值 null + onSearchPick 落账字面（含 terms 缺席/空两态防呆）+ 先落账后导航。"""
    app = _js("js/app.js")
    assert re.search(r"searchHighlight:\s*null,", app), "state.searchHighlight 初值 null 缺位"
    m = re.search(r"function onSearchPick\(path, heading, terms\) \{(.*?)\n\}", app, re.S)
    assert m, "onSearchPick(path, heading, terms) 三参形缺位（pick item terms 传参通道）"
    seg = m.group(1)
    assert "state.searchHighlight = { path, terms:" in seg, "落账字面 { path, terms } 缺位"
    assert "Array.isArray(terms)" in seg, "terms 缺席/非数组防呆缺位"
    assert "searchNavGrant = path" in seg, "一次性放行令牌发放缺位"
    assert seg.index("state.searchHighlight") < seg.index("openDoc("), \
        "落账必须先于 openDoc（docContent 更新才是重渲染扳机，落账须先就位）"
    # 两条 pick 链（桌面页 + 移动 sheet）共绑同一 handler → 一处落账两壳同接
    assert app.count(':on-pick="onSearchPick"') == 2, "pick 链应恰为桌面/移动两处绑定"


def test_app_zeroing_three_points():
    """三处清零点字面：① openDoc 非搜索来源；② setMode 编辑支；③ watch previewMode 兜底旁路。"""
    app = _js("js/app.js")
    # 模块级单盒声明须先于 openDoc（let 无提升，TDZ 不容逆行）
    assert app.index("let searchNavGrant") < app.index("async function openDoc"), \
        "searchNavGrant 声明晚于 openDoc（TDZ）"
    # ① openDoc 非搜索来源（文件树/链接/hash 深链）清零 + 令牌读后即清
    mo = re.search(r"async function openDoc\(path, heading, fromLink\) \{(.*?)\n\}", app, re.S)
    assert mo, "openDoc 段缺失"
    seg = mo.group(1)
    assert "if (searchNavGrant !== path) state.searchHighlight = null" in seg, \
        "openDoc 非搜索来源清零缺位"
    assert "searchNavGrant = null" in seg, "令牌读后即清缺位（跨导航残留=同路径假放行）"
    assert seg.index("searchNavGrant = null") < seg.index("state.docContent = text"), \
        "清零须先于 docContent 更新（重渲染扳机）"
    # ② setMode 编辑支清零
    ms = re.search(r"function setMode\(mode\) \{(.*?)\n\}", app, re.S)
    assert ms, "setMode 段缺失"
    assert "if (mode === 'edit') state.searchHighlight = null" in ms.group(1), \
        "setMode 编辑态清零缺位"
    # ③ 旁路兜底：landOn/landCell 直置 previewMode=false 不经 setMode → watch 统灭
    assert re.search(
        r"watch\(\(\) => state\.previewMode, \(on\) => \{ if \(!on\) state\.searchHighlight = null \}\)",
        app,
    ), "watch(previewMode) 编辑态旁路兜底清零缺位（EditorPane landOn/landCell 直写面）"


# ────────────────────────── ③ EditorPane 挂点 ──────────────────────────

def test_editorpane_mount_order_between_post_and_mapping():
    """挂点契约（spec §4）：postProcessPreview 之后、writeSrcLineMapping 之前。"""
    ep = _js("js/components/EditorPane.js")
    assert "import { highlightDocTerms, clearDocHits } from '../utils/bodyHighlight.js'" in ep, \
        "bodyHighlight 未 import（含清除面 clearDocHits）"
    m = re.search(r"async function refreshPreview\(\) \{(.*?)\n    \}", ep, re.S)
    assert m, "refreshPreview 段缺失"
    seg = m.group(1)
    i_post = seg.index("postProcessPreview(previewHtmlEl.value)")
    i_hl = seg.index("applySearchHighlight()")
    i_map = seg.index("writeSrcLineMapping(")
    assert i_post < i_hl < i_map, \
        "高亮 pass 须夹在 postProcessPreview 与 writeSrcLineMapping 之间（hljs/mermaid 终形后、行映射写前）"
    # helper 三守卫：落账在场 + path 命中当前文档 + terms 非空
    mh = re.search(r"function applySearchHighlight\(\) \{(.*?)\n    \}", ep, re.S)
    assert mh, "applySearchHighlight helper 缺失"
    h = mh.group(1)
    assert "state.searchHighlight" in h, "helper 缺 state.searchHighlight 读取"
    assert "hl.path !== state.currentPath" in h, "缺当前文档路径守卫"
    assert re.search(r"hl\.terms\.length", h), "缺空词元守卫"
    assert "highlightDocTerms(previewHtmlEl.value, hl.terms)" in h, "helper 未实际调高亮工具"


def test_editorpane_dom_clear_watch_on_ledger_null():
    """联调返工 2026-11-24（双向收口）：app.js 清零只落 state 面——预览 v-show 常驻 +
    renderedHtml 输入未变 → v-html 不重 patch、refreshPreview 不跑（编辑↔预览往返旧
    .doc-hit 留亮实证；同文档「进编辑熄灭→再点同一条结果」则反向熄灭不补亮）。
    EditorPane watch 跟落账双向同步：变 null 即 clearDocHits 解包；非 null 且
    path=当前文档即 applySearchHighlight 补打（异 path 归 refreshPreview 重打面，禁双打）。"""
    ep = _js("js/components/EditorPane.js")
    # watch getter 须带 state 在场防呆（inject('state', null) 可空面）
    m = re.search(r"watch\(\(\) => state && state\.searchHighlight, \(hl\) => \{(.+?)\n    \}\)", ep, re.S)
    assert m, "watch(state.searchHighlight) DOM 同步面缺位（getter 且须含 state 防呆）"
    body = m.group(1)
    # ① 清零面：null 早退支 + 预览 DOM 在场守卫
    assert re.search(r"if \(!hl\) \{\s*\n\s*if \(previewHtmlEl\.value\) clearDocHits\(previewHtmlEl\.value\)\s*\n\s*return\s*\n\s*\}", body), \
        "清零面缺位（if (!hl) 早退 + clearDocHits 调用 + return 分隔两相）"
    # ② 重打面：非 null 分支 path 守卫（异 path 禁双打）+ applySearchHighlight 字面 + DOM 在场守卫
    assert "if (hl.path === state.currentPath && previewHtmlEl.value) applySearchHighlight()" in body, \
        "重打面缺位（非 null 分支须 path 相等才打 + 预览 DOM 在场守卫 + applySearchHighlight 字面）"


# ────────────────────────── ④ CSS 在场钉（预期红） ──────────────────────────

def test_doc_hit_css_present():
    """⚠ 预期红（合流前）：.doc-hit 规则由主会话落 style.css（本 lane 禁改 CSS），落盘即转绿。

    存在性引信钉：防「JS 已挂类、CSS 缺席」的静默哑火。底色口径 = brass 22% 底家族
    （spec §4，对齐 .sp-snip mark.hit-first 混色参数；正文不分 first/subsequent——
    color-mix 主版/rgba 回退版二选一均可过此钉，故只钉 类名+background 在场）。
    """
    css = (TPL / "style.css").read_text(encoding="utf-8")
    m = re.search(r"\.doc-hit\s*\{[^}]*\}", css)
    assert m, ".doc-hit 规则未落盘（主会话合流 CSS 后转绿——本钉合流前预期红，见 docstring）"
    assert "background" in m.group(0), ".doc-hit 须含底色声明（brass 22% 底家族口径）"
