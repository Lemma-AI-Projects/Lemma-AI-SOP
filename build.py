#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 把仓库根目录的中文 Markdown 渲染成可静态访问的网站，输出到 site/。

设计约束
    * 只用 Python 3 标准库：不联网、不需要 pip install / npm install。
    * 源 Markdown **只以只读方式打开**，绝不写入、不改写、不重排、不补字。
    * 可重复执行：每次清空并重建 site/。
    * site/ 里的页面直接双击即可打开（不依赖本地服务器，无任何外部网络请求）。

用法（在仓库根目录）
    python build.py

新增一份文档时，只要在下面的 PAGES 表里加一行即可。
"""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "site"

SITE_NAME = "Lemma AI SOP"
TAGLINE = "团队如何思考，以及信息如何流转。"
FOOTER = "by ceaserzhao"

STYLESHEET = "style.css"

# ---------------------------------------------------------------------------
# 源文件 → 输出页面 的对应关系表
#   src   仓库里的源 Markdown 文件名（原样，含中文与空格）
#   out   生成的 HTML 文件名
#   nav   顶部导航里的标签
#   title 浏览器标签页标题
#   crumb 正文顶部小字里「当前在看」的名称
#   home  首页六个入口里的名称
# ---------------------------------------------------------------------------
PAGES = [
    {"src": "README.md",        "out": "overview.html", "nav": "总览",
     "title": "总览",       "crumb": "总览 · README",   "home": "总览"},
    {"src": "SOP-1 新功能.md",   "out": "sop-1.html",    "nav": "1",
     "title": "新功能",     "crumb": "SOP-1 · 新功能",   "home": "新功能"},
    {"src": "SOP-2 任务分配.md", "out": "sop-2.html",    "nav": "2",
     "title": "任务分配",   "crumb": "SOP-2 · 任务分配", "home": "任务分配"},
    {"src": "SOP-3 战略分析.md", "out": "sop-3.html",    "nav": "3",
     "title": "战略分析",   "crumb": "SOP-3 · 战略分析", "home": "战略分析"},
    {"src": "SOP-4 元帮助.md",   "out": "sop-4.html",    "nav": "4",
     "title": "元帮助",     "crumb": "SOP-4 · 元帮助",   "home": "元帮助"},
    {"src": "术语与约定.md",      "out": "glossary.html", "nav": "术语",
     "title": "术语与约定", "crumb": "术语与约定",        "home": "术语与约定"},
]

HOME_OUT = "index.html"

EN_SCRIPT = """<script>
(function () {
  var btn = document.getElementById('en-btn');
  var note = document.getElementById('en-note');
  if (!btn || !note) return;
  btn.addEventListener('click', function () {
    var willShow = note.hasAttribute('hidden');
    if (willShow) { note.removeAttribute('hidden'); }
    else { note.setAttribute('hidden', ''); }
    btn.setAttribute('aria-expanded', willShow ? 'true' : 'false');
  });
})();
</script>"""


# ===========================================================================
# 一、Markdown → HTML
# ===========================================================================

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`~]*)\s*$")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
QUOTE_RE = re.compile(r"^\s*>")
LIST_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
ORDER_RE = re.compile(r"^(\s*)(\d+)\.(?:[ \t]+|(?=[\u4e00-\u9fff\u3000-\u303f]))(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"']([^\"']*)[\"'])?\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"']([^\"']*)[\"'])?\)")
BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(?=\S)([^*\n]+?)(?<=\S)\*(?!\*)")
HIGHLIGHT_RE = re.compile(r"(?<!=)==(?!=)(.+?)(?<!=)==(?!=)")
PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")

# ASCII 图里用到的制表线（U+2502 等，注意与 ASCII 竖线 | 不同）与箭头字符
BOX_CHARS = set("│┌┐└┘├┤┬┴┼─━")
ARROW_CHARS = set("↑↓←→")
ART_CHARS = BOX_CHARS | ARROW_CHARS


def leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_art_line(line: str) -> bool:
    """判断这一行是否像 ASCII 图的一部分（缩进 ≥4 空格，或含制表线／箭头字符）。"""
    s = line.strip()
    if not s:
        return False
    if s[0] in ">|#":
        return False
    if s.startswith(("- ", "* ", "+ ")):
        return False
    if HR_RE.match(line):
        return False
    if leading_spaces(line) >= 4:
        return True
    return any(ch in line for ch in ART_CHARS)


def has_box_char(line: str) -> bool:
    """只判断制表线字形；箭头单独判断，避免把"正文里的 → "误当成图。"""
    return any(ch in line for ch in BOX_CHARS)


def has_arrow(line: str) -> bool:
    return any(ch in line for ch in ARROW_CHARS)


def pre_block(lines: list[str], i: int):
    """若从第 i 行起应放进 <pre>，返回 (类型, 行列表)；否则返回 None。

    类型：
      "figure"  ASCII 图：连续 ≥2 行"图形行"，且其中至少一行含制表线，
                或"含箭头且整块有缩进"（README 的 [想法]→…→[对外] 小图属于后者）。
                这样未加围栏的结构图能整块保留，而正文里成对的 **… → …** 句子不会被误判。
      "indent"  纯缩进代码块：行首缩进 ≥4 空格（标准 Markdown 写法）。
                README 末尾的日期与落款属于这一类——它们缩进深达 42 空格，
                窄屏需要单独的字号处理（见 style.css 的 .pre-indent）。
    """
    line = lines[i]

    if is_art_line(line):
        run: list[str] = []
        j = i
        while j < len(lines) and is_art_line(lines[j]):
            run.append(lines[j])
            j += 1
        looks_drawn = any(has_box_char(x) for x in run) or (
            any(has_arrow(x) for x in run) and any(leading_spaces(x) >= 4 for x in run)
        )
        if len(run) >= 2 and looks_drawn:
            return "figure", run

    if leading_spaces(line) >= 4:
        run = []
        j = i
        while j < len(lines) and lines[j].strip() and leading_spaces(lines[j]) >= 4:
            run.append(lines[j])
            j += 1
        if run:
            return "indent", run

    return None


def is_table_start(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    if "|" not in lines[i]:
        return False
    sep = lines[i + 1]
    return "-" in sep and bool(TABLE_SEP_RE.match(sep))


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def rewrite_href(href: str) -> str:
    """把指向源 .md 的链接改写到对应的生成页面，避免站内死链。"""
    key = urllib.parse.unquote(href).lstrip("./")
    for page in PAGES:
        if key == page["src"]:
            return page["out"]
    if key == "README.md":
        return "overview.html"
    return href


def inline(text: str) -> str:
    """行内渲染：先摘出行内代码，转义 HTML，再处理其余标记。"""
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(2))
        return "\x00%d\x00" % (len(spans) - 1)

    text = CODE_SPAN_RE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = HIGHLIGHT_RE.sub(r"<mark>\1</mark>", text)

    def image_tag(m: re.Match) -> str:
        alt, src, title = m.group(1), m.group(2), m.group(3)
        parts = ['<img src="%s"' % html.escape(rewrite_href(src), quote=True),
                 ' alt="%s"' % html.escape(alt, quote=True)]
        if title:
            parts.append(' title="%s"' % html.escape(title, quote=True))
        return "".join(parts) + ">"

    def link_tag(m: re.Match) -> str:
        label, href, title = m.group(1), m.group(2), m.group(3)
        parts = ['<a href="%s"' % html.escape(rewrite_href(href), quote=True)]
        if title:
            parts.append(' title="%s"' % html.escape(title, quote=True))
        return "".join(parts) + ">%s</a>" % label

    text = IMAGE_RE.sub(image_tag, text)
    text = LINK_RE.sub(link_tag, text)
    text = BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = ITALIC_RE.sub(r"<em>\1</em>", text)

    def unstash(m: re.Match) -> str:
        return "<code>%s</code>" % html.escape(spans[int(m.group(1))], quote=False)

    return PLACEHOLDER_RE.sub(unstash, text)


def starts_block(lines: list[str], i: int) -> bool:
    """判断第 i 行是否会开启一个新的块（用于结束段落收集）。"""
    line = lines[i]
    if not line.strip():
        return True
    if FENCE_RE.match(line) or HEAD_RE.match(line) or HR_RE.match(line):
        return True
    if QUOTE_RE.match(line):
        return True
    if is_table_start(lines, i):
        return True
    if LIST_RE.match(line) or ORDER_RE.match(line):
        return True
    return pre_block(lines, i) is not None


def render_markdown(md: str) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # --- 围栏代码块 ```text ---
        fence = FENCE_RE.match(line)
        if fence:
            body: list[str] = []
            i += 1
            while i < n and not re.match(r"^\s*(`{3,}|~{3,})\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # 跳过收尾围栏
            out.append("<pre><code>%s</code></pre>"
                       % html.escape("\n".join(body), quote=False))
            continue

        # --- 水平线 ---
        if HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        # --- 标题 ---
        head = HEAD_RE.match(line)
        if head:
            level = len(head.group(1))
            out.append("<h%d>%s</h%d>" % (level, inline(head.group(2)), level))
            i += 1
            continue

        # --- 引用块 ---
        if QUOTE_RE.match(line):
            body = []
            while i < n and QUOTE_RE.match(lines[i]):
                body.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            while body and not body[-1].strip():
                body.pop()
            out.append("<blockquote><p>%s</p></blockquote>"
                       % "<br>".join(inline(x) for x in body if x.strip()))
            continue

        # --- 表格 ---
        if is_table_start(lines, i):
            header = split_row(lines[i])
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(split_row(lines[i]))
                i += 1
            width = len(header)
            cells = "".join("<th>%s</th>" % inline(c) for c in header)
            body_rows = []
            for row in rows:
                row = (row + [""] * width)[:width]
                body_rows.append("<tr>%s</tr>"
                                 % "".join("<td>%s</td>" % inline(c) for c in row))
            out.append(
                '<div class="table-wrap"><table><thead><tr>%s</tr></thead>'
                "<tbody>%s</tbody></table></div>" % (cells, "".join(body_rows))
            )
            continue

        # --- 列表 ---
        ul = LIST_RE.match(line)
        ol = ORDER_RE.match(line)
        if ul or ol:
            ordered = ol is not None
            items: list[list[str]] = []
            while i < n:
                u = LIST_RE.match(lines[i])
                o = ORDER_RE.match(lines[i])
                m = o if ordered else u
                if m is None:
                    break
                items.append([m.group(3)])
                i += 1
                # 缩进 ≥2 的后续行并入当前条目
                while (i < n and lines[i].strip()
                       and not LIST_RE.match(lines[i])
                       and not ORDER_RE.match(lines[i])
                       and leading_spaces(lines[i]) >= 2):
                    items[-1].append(lines[i].strip())
                    i += 1
            tag = "ol" if ordered else "ul"
            body_html = "".join(
                "<li>%s</li>" % "<br>".join(inline(x) for x in item)
                for item in items
            )
            out.append("<%s>%s</%s>" % (tag, body_html, tag))
            continue

        # --- ASCII 图 / 缩进代码块 ---
        block = pre_block(lines, i)
        if block is not None:
            kind, block_lines = block
            i += len(block_lines)
            cls = ' class="pre-indent"' if kind == "indent" else ""
            out.append("<pre%s><code>%s</code></pre>"
                       % (cls, html.escape("\n".join(block_lines), quote=False)))
            continue

        # --- 段落（源文件是 Obsidian 写的，段落内换行按硬换行处理）---
        para = [line]
        i += 1
        while i < n and not starts_block(lines, i):
            para.append(lines[i])
            i += 1
        out.append("<p>%s</p>" % "<br>".join(inline(x) for x in para))

    return "\n".join(out)


# ===========================================================================
# 二、页面骨架
# ===========================================================================

def topbar(active_out: str | None) -> str:
    items = []
    for page in PAGES:
        current = ' aria-current="page"' if page["out"] == active_out else ""
        items.append('<a href="%s"%s title="%s">%s</a>'
                     % (page["out"], current,
                        html.escape(page["home"]), html.escape(page["nav"])))
    return "\n".join([
        '<header class="topbar">',
        '  <div class="topbar-inner">',
        '    <nav class="nav" aria-label="站点导航">%s</nav>'
        % '<span class="sep">·</span>'.join(items),
        '    <button type="button" class="en-btn" id="en-btn"'
        ' aria-controls="en-note" aria-expanded="false">EN</button>',
        '  </div>',
        '  <p class="en-note" id="en-note" hidden>英文版尚未提供</p>',
        '</header>',
    ])


def document(title: str, nav_active: str | None, main_class: str, inner: str) -> str:
    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s</title>" % html.escape(title),
        '<link rel="icon" href="data:,">',
        '<link rel="stylesheet" href="%s">' % STYLESHEET,
        "</head>",
        "<body>",
        topbar(nav_active),
        '<main class="%s">' % main_class,
        inner,
        "</main>",
        '<footer class="site-footer">%s</footer>' % FOOTER,
        EN_SCRIPT,
        "</body>",
        "</html>",
        "",
    ])


def build_home() -> str:
    links = "\n".join(
        '    <a href="%s">%s</a>' % (p["out"], html.escape(p["home"]))
        for p in PAGES
    )
    inner = "\n".join([
        '  <h1 class="home-title">%s</h1>' % html.escape(SITE_NAME),
        '  <p class="home-tagline">%s</p>' % html.escape(TAGLINE),
        '  <nav class="entries" aria-label="文档入口">',
        links,
        '  </nav>',
    ])
    return document(SITE_NAME, None, "home", inner)


def build_page(page: dict, body: str) -> str:
    if page["out"] == "overview.html":
        meta = ('当前在看：%s · <a href="%s">首页</a>'
                % (html.escape(page["crumb"]), HOME_OUT))
    else:
        meta = ('当前在看：%s · <a href="overview.html">返回总览</a> · '
                '<a href="%s">首页</a>'
                % (html.escape(page["crumb"]), HOME_OUT))
    inner = "\n".join([
        '  <p class="doc-meta">%s</p>' % meta,
        "  <article>",
        body,
        "  </article>",
    ])
    return document("%s · %s" % (page["title"], SITE_NAME), page["out"], "doc", inner)


# ===========================================================================
# 三、主流程
# ===========================================================================

def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    print("=" * 68)
    print("%s — 站点构建" % SITE_NAME)
    print("=" * 68)

    # 1. 校验源文件都在
    missing = [p["src"] for p in PAGES if not (ROOT / p["src"]).is_file()]
    if missing:
        print("[错误] 缺少源文件：%s" % "、".join(missing))
        return 1

    stylesheet_src = ROOT / STYLESHEET
    if not stylesheet_src.is_file():
        print("[错误] 缺少样式表：%s" % STYLESHEET)
        return 1

    # 2. 源文件只读读入（模式 "r"，不写回）
    print("\n源文件（只读，SHA-256 前 12 位）")
    sources: dict[str, str] = {}
    for page in PAGES:
        path = ROOT / page["src"]
        text = path.read_text(encoding="utf-8")     # 只读
        sources[page["src"]] = text
        bom = path.read_bytes().startswith(b"\xef\xbb\xbf")
        print("  %-20s %s  %6d 字符  编码 UTF-8%s"
              % (page["src"], sha256_of(path), len(text),
                 "（含 BOM！）" if bom else " 无 BOM"))

    # 3. 清空并重建 site/
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    written: list[tuple[str, int]] = []

    def emit(name: str, text: str) -> None:
        data = text.encode("utf-8")
        (OUT_DIR / name).write_bytes(data)
        written.append((name, len(data)))

    # 4. 样式表原样复制
    emit(STYLESHEET, stylesheet_src.read_text(encoding="utf-8"))

    # 5. 首页
    emit(HOME_OUT, build_home())

    # 6. 六份内容页
    for page in PAGES:
        body = render_markdown(sources[page["src"]])
        emit(page["out"], build_page(page, body))

    # 7. 报告
    print("\n输出（%s/）" % OUT_DIR.name)
    for name, size in written:
        print("  %-18s %7d 字节" % (name, size))
    print("\n完成：%d 个页面 + 1 个样式表。" % (len(written) - 1))
    print("源 Markdown 未被写入或修改。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
