"""前端静态资产的结构守卫（D17：frontend 自有分层，原型目录不参与运行）。

这些用例只读仓库文件，不需要数据库与模型：把"目录/分层/令牌"的约定变成可执行的检查，
避免下一次复制页面时又把样式和适配层塞回同一个扁平目录。
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FRONTEND = _REPO_ROOT / "frontend"
_ADMIN_PAGES = _FRONTEND / "admin" / "pages"
_WIDGET = _FRONTEND / "widget"

# 页面里出现这些名字，说明还在引用旧扁平目录（D17 之前的结构）
_LEGACY_REFS = ('href="styles.css"', 'src="real-api.js"', 'src="layout.js"',
                'src="app.js"', 'src="preview.js"', 'src="api.js"',
                'src="loader.js"')

_VAR_USE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")  # 只查无兜底值的引用
_VAR_DEF = re.compile(r"(--[a-z0-9-]+)\s*:")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_no_legacy_refs(html: str, name: str) -> None:
    for ref in _LEGACY_REFS:
        # widget 根目录的页面允许引用同目录 loader.js（宿主接入层的固定入口）
        if name.startswith("widget") and ref == 'src="loader.js"':
            continue
        assert ref not in html, f"{name} 仍引用旧结构：{ref}"


def test_design_tokens_are_served_and_shared() -> None:
    """设计令牌是两端唯一来源，且由 app 同源托管。"""
    tokens = _FRONTEND / "design" / "tokens.css"
    assert tokens.is_file()
    text = _read(tokens)
    for token in ("--green:#0f7a52", "--ink:#1f2933", "--font:"):
        assert token in text
    client = TestClient(create_app())
    resp = client.get("/ui-design/tokens.css")
    assert resp.status_code == 200
    assert "--green:#0f7a52" in resp.text


def test_favicon_declared_and_served() -> None:
    """浏览器标签页图标：每页都声明 SVG 图标，且 /favicon.ico 不再 404。"""
    icon = _FRONTEND / "admin" / "favicon.svg"
    assert icon.is_file(), "后台缺少 favicon.svg"
    svg = _read(icon)
    assert 'viewBox="0 0 32 32"' in svg
    assert "--green" not in svg and "#b25c2a" in svg, "图标底应取品牌主色（橙）"

    pages = sorted((_FRONTEND / "admin" / "pages").glob("*.html"))
    assert pages, "后台页面缺失"
    for page in pages:
        html = _read(page)
        assert 'rel="icon"' in html and "favicon.svg" in html, f"{page.name} 没有声明图标"

    client = TestClient(create_app())
    served = client.get("/admin-app/favicon.svg")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/svg+xml")
    # 浏览器默认还会请求 /favicon.ico：重定向到同一枚图标，避免日志噪音
    redirect = client.get("/favicon.ico", follow_redirects=False)
    assert redirect.status_code in (301, 302, 307)
    assert redirect.headers["location"].endswith("/admin-app/favicon.svg")


def test_static_assets_always_revalidate() -> None:
    """静态资源必须回源校验：否则改样式后用户看到的还是缓存里的旧版本。"""
    client = TestClient(create_app())
    for url in ("/admin-app/styles/admin.css", "/admin-app/pages/index.html",
                "/ui-design/tokens.css"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert resp.headers.get("cache-control") == "no-cache", f"{url} 缺少 no-cache"


_ASSET_REF = re.compile(
    r'(?:src|href)="(\.\./(?:controllers|adapters|styles)/[^"]+\.(?:js|css))"'
)


def test_admin_pages_version_their_assets() -> None:
    """静态资源必须带 ?v=：改 JS/CSS 后浏览器重新取，避免新旧脚本混用（线上踩过）。"""
    pages = sorted(_ADMIN_PAGES.glob("*.html"))
    assert pages
    for page in pages:
        for match in _ASSET_REF.finditer(_read(page)):
            url = match.group(1)
            assert "?v=" in url, f"{page.name} 静态资源缺少版本号：{url}"


def test_faq_import_template_is_served_and_linked() -> None:
    """FAQ 导入模板：静态资产可取到（xlsx 二进制），且上传弹窗给出下载入口。"""
    template = _FRONTEND / "admin" / "assets" / "faq-import-template.xlsx"
    assert template.is_file(), "后台缺少 FAQ 导入模板"

    html = _read(_ADMIN_PAGES / "index.html")
    assert "faq-import-template.xlsx" in html, "上传弹窗没有提供模板下载链接"
    assert "下载FAQ模版" in html, "下载入口文案缺失"
    assert 'class="hint-sub"' in html, "下载入口要独占一行（不能塞进 flex 的 .hint 里）"

    client = TestClient(create_app())
    served = client.get("/admin-app/assets/faq-import-template.xlsx")
    assert served.status_code == 200
    assert served.content[:2] == b"PK", "xlsx 应是 zip 容器"
    assert served.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ), "xlsx 不能被当成 octet-stream 下载"


def test_static_assets_reachable_under_mounts() -> None:
    """后台页面的 ../styles、../controllers 等相对路径必须真的能取到。"""
    client = TestClient(create_app())
    for url in (
        "/admin-app/styles/admin.css",
        "/admin-app/adapters/real-api.js",
        "/admin-app/controllers/app.js",
        "/admin-app/pages/index.html",
        "/admin-app/pages/conversations.html",
        "/admin-app/pages/conversation.html",
        "/admin-app/pages/gaps.html",
        "/admin-app/pages/settle.html",
        "/admin-app/pages/users.html",
        "/admin-app/pages/account.html",
        "/admin-app/pages/my-qa.html",
        "/admin-app/pages/my-save.html",
        "/admin-app/pages/config.html",
        "/admin-app/pages/eval.html",
        "/admin-app/pages/dashboard.html",
        "/admin-app/adapters/logs-api.js",
        "/admin-app/adapters/conversations-api.js",
        "/admin-app/adapters/settle-api.js",
        "/admin-app/adapters/users-api.js",
        "/admin-app/adapters/config-api.js",
        "/admin-app/adapters/evals-api.js",
        "/admin-app/controllers/conversations.js",
        "/admin-app/controllers/conversation.js",
        "/admin-app/controllers/gaps.js",
        "/admin-app/controllers/settle.js",
        "/admin-app/controllers/users.js",
        "/admin-app/controllers/account.js",
        "/admin-app/controllers/config.js",
        "/admin-app/controllers/my-qa.js",
        "/admin-app/controllers/my-save.js",
        "/admin-app/controllers/theme-boot.js",
        "/admin-app/controllers/eval.js",
        "/admin-app/controllers/dashboard.js",
        "/widget-app/styles/widget.css",
        "/widget-app/styles/demo-console.css",
        "/widget-app/adapters/api.js",
        "/widget-app/controllers/app.js",
    ):
        assert client.get(url).status_code == 200, f"{url} 不可达"
    # 根路径 307 到 pages/，浏览器跟着跳转后能拿到登录入口
    root = client.get("/admin-app/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/admin-app/pages/"


def test_pages_load_tokens_and_layered_assets() -> None:
    """所有页面先加载共用令牌，再加载各自的样式/控制器/适配器。"""
    for page in sorted(_ADMIN_PAGES.glob("*.html")):
        html = _read(page)
        assert "/ui-design/tokens.css" in html, f"{page.name} 未引设计令牌"
        assert "../controllers/theme-boot.js" in html, f"{page.name} 缺首屏主题恢复脚本"
        assert "../styles/admin.css" in html
        assert "../adapters/real-api.js" in html
        _assert_no_legacy_refs(html, page.name)
        if page.name != "login.html":  # 登录页没有侧栏/预览脚本
            assert "../controllers/layout.js" in html
            assert "../controllers/app.js" in html
            assert "../controllers/preview.js" in html

    for page in (("index.html", "widget"), ("widget.html", "widget")):
        html = _read(_WIDGET / page[0])
        assert "/ui-design/tokens.css" in html, f"{page[0]} 未引设计令牌"
        assert "styles/widget.css" in html
        _assert_no_legacy_refs(html, page[0])

    widget_html = _read(_WIDGET / "widget.html")
    assert "adapters/api.js" in widget_html
    assert "controllers/app.js" in widget_html
    assert (style := _WIDGET / "styles" / "widget.css").is_file() and style


def test_every_css_var_resolves() -> None:
    """样式里用到的每个 var(--x) 都必须在令牌或本端样式里有定义。"""
    tokens_defs = set(_VAR_DEF.findall(_read(_FRONTEND / "design" / "tokens.css")))
    for css in (_FRONTEND / "admin" / "styles" / "admin.css",
                _FRONTEND / "widget" / "styles" / "widget.css"):
        text = _read(css)
        own_defs = set(_VAR_DEF.findall(text))
        used = set(_VAR_USE.findall(text))
        missing = used - own_defs - tokens_defs
        assert not missing, f"{css.name} 存在未定义令牌：{sorted(missing)}"


def test_frontend_does_not_read_prototype_dirs() -> None:
    """实现不依赖原型目录：运行文件里不得把原型路径当资源引用（D17）。

    注释里可以写"来源：已确认原型"，但 src/href/fetch 之类的资源路径不能指向原型目录，
    否则原型目录一旦删除，前端就失效。
    """
    runtime_files = [* _ADMIN_PAGES.glob("*.html"),
                     * (_FRONTEND / "admin").rglob("*.js"),
                     * _WIDGET.rglob("*.html"),
                     * _WIDGET.rglob("*.js"),
                     * _WIDGET.rglob("*.css"),
                     * (_FRONTEND / "admin").rglob("*.css")]
    quoted_prototype = re.compile(r"""["'](?:\.\./)*(?:admin|widget)-prototype/""")
    for path in runtime_files:
        text = _read(path)
        assert not quoted_prototype.search(text), f"{path} 把原型目录当资源引用"
    # 服务端挂载是运行链路的入口，同样不得把原型目录当资源路径
    main_py = _read(_REPO_ROOT / "app" / "main.py")
    assert not quoted_prototype.search(main_py), "app/main.py 把原型目录当资源路径"
    assert '"frontend"' in main_py, "app/main.py 应从 frontend/ 托管静态站"


def test_widget_demo_iframe_reload_keeps_demo_code() -> None:
    """演示页重载 iframe（如关掉「模拟服务不可达」）必须带上 ?code=。

    否则重载后的 iframe 没有入口码，建会话会 401，界面掉回"需要从官网进入"的拦截页
    ——这类"关掉故障模拟反而被门禁拦住"的问题只有在真实点击路径上才会暴露。
    """
    html = _read(_WIDGET / "index.html")
    assert "DEMO_CODE" in html
    assert re.search(r"function frameSrc\(server\)", html)
    assert re.search(r"URLSearchParams\(location\.search\)\.get\('code'\)", html)
    reload_start = html.index("function reloadFrame")
    reload_block = html[reload_start : reload_start + 400]
    assert "frameSrc(" in reload_block, "reloadFrame 必须用 frameSrc 构造地址（保留入口码）"
    assert "'widget.html?server=' +" not in reload_block, "reloadFrame 不能再手拼不带 code 的地址"
