"""Web page for mobile read-only browsing."""
from __future__ import annotations

_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>md-sync 文档</title>
<style>
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --panel: #f5f5f7;
  --border: #e0e0e0;
  --accent: #2563eb;
  --muted: #6b7280;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212;
    --fg: #e5e5e5;
    --panel: #1e1e1e;
    --border: #333333;
    --accent: #60a5fa;
    --muted: #9ca3af;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--fg);
}
#app { display: flex; height: 100vh; }
#tree-panel {
  width: 260px;
  min-width: 180px;
  border-right: 1px solid var(--border);
  background: var(--panel);
  overflow-y: auto;
  padding: 8px;
}
#content-panel { flex: 1; overflow-y: auto; padding: 16px; }
#content-panel h1 { font-size: 1.4em; margin-top: 0; }
#content-panel pre { background: var(--panel); padding: 12px; border-radius: 6px; overflow-x: auto; }
#content-panel code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
#content-panel table { border-collapse: collapse; margin: 12px 0; }
#content-panel th, #content-panel td { border: 1px solid var(--border); padding: 6px 10px; }
#content-panel blockquote { border-left: 4px solid var(--accent); margin: 12px 0; padding: 4px 12px; color: var(--muted); }
#content-panel img { max-width: 100%; }
.tree-node { user-select: none; }
.tree-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
}
.tree-row:hover { background: rgba(128,128,128,0.15); }
.tree-row.active { background: var(--accent); color: #fff; }
.tree-row .arrow { width: 14px; display: inline-block; color: var(--muted); }
.tree-row.active .arrow { color: #fff; }
.tree-children { margin-left: 16px; }
#toolbar { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }
#toolbar button {
  background: var(--accent);
  color: #fff;
  border: none;
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}
#toolbar button:disabled { opacity: 0.5; cursor: default; }
#placeholder { color: var(--muted); margin-top: 40px; text-align: center; }
#status { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
@media (max-width: 700px) {
  #app { flex-direction: column; }
  #tree-panel { width: 100%; max-height: 40vh; border-right: none; border-bottom: 1px solid var(--border); }
}
</style>
</head>
<body>
<div id="app">
  <div id="tree-panel"><div id="status">加载中…</div><div id="tree"></div></div>
  <div id="content-panel">
    <div id="toolbar" style="display:none">
      <button id="btn-download">下载 .md</button>
    </div>
    <div id="placeholder">从左侧选择一个文档开始阅读</div>
    <article id="doc" style="display:none"></article>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
(function () {
  var treeEl = document.getElementById('tree');
  var statusEl = document.getElementById('status');
  var docEl = document.getElementById('doc');
  var placeholderEl = document.getElementById('placeholder');
  var toolbarEl = document.getElementById('toolbar');
  var btnDownload = document.getElementById('btn-download');
  var currentPath = null;

  function buildTree(files) {
    var root = {};
    files.forEach(function (f) {
      var parts = f.path.split('/');
      var node = root;
      for (var i = 0; i < parts.length - 1; i++) {
        var name = parts[i];
        if (!node[name]) node[name] = { __dirs: {}, __files: [] };
        node = node[name];
      }
      var fileName = parts[parts.length - 1];
      node.__files.push({ name: fileName, path: f.path });
    });

    function render(node, container) {
      var dirs = Object.keys(node).filter(function (k) { return k !== '__dirs' && k !== '__files'; });
      dirs.sort();
      dirs.forEach(function (d) {
        var wrap = document.createElement('div');
        wrap.className = 'tree-node';
        var row = document.createElement('div');
        row.className = 'tree-row';
        var arrow = document.createElement('span');
        arrow.className = 'arrow';
        arrow.textContent = '▾';
        var label = document.createElement('span');
        label.textContent = d;
        row.appendChild(arrow);
        row.appendChild(label);
        var children = document.createElement('div');
        children.className = 'tree-children';
        row.addEventListener('click', function () {
          var collapsed = children.style.display === 'none';
          children.style.display = collapsed ? '' : 'none';
          arrow.textContent = collapsed ? '▾' : '▸';
        });
        wrap.appendChild(row);
        wrap.appendChild(children);
        render(node[d], children);
        container.appendChild(wrap);
      });
      var files = node.__files || [];
      files.sort(function (a, b) { return a.name < b.name ? -1 : 1; });
      files.forEach(function (f) {
        var row = document.createElement('div');
        row.className = 'tree-row';
        var arrow = document.createElement('span');
        arrow.className = 'arrow';
        arrow.textContent = '·';
        var label = document.createElement('span');
        label.textContent = f.name;
        row.appendChild(arrow);
        row.appendChild(label);
        row.addEventListener('click', function () {
          document.querySelectorAll('.tree-row.active').forEach(function (el) { el.classList.remove('active'); });
          row.classList.add('active');
          openDoc(f.path);
        });
        container.appendChild(row);
      });
    }

    treeEl.innerHTML = '';
    render(root, treeEl);
  }

  function openDoc(path) {
    currentPath = path;
    placeholderEl.style.display = 'none';
    docEl.style.display = '';
    toolbarEl.style.display = 'flex';
    btnDownload.disabled = true;
    docEl.innerHTML = '<p style="color:var(--muted)">加载中…</p>';
    fetch('/api/docs/' + encodeURIComponent(path))
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (text) {
        docEl.innerHTML = marked.parse(text);
        btnDownload.disabled = false;
      })
      .catch(function (err) {
        docEl.innerHTML = '<p style="color:#e11d48">加载失败: ' + err.message + '</p>';
      });
  }

  btnDownload.addEventListener('click', function () {
    if (!currentPath) return;
    var a = document.createElement('a');
    a.href = '/api/docs/' + encodeURIComponent(currentPath);
    a.download = currentPath.split('/').pop();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  });

  function loadDocs() {
    fetch('/api/docs')
      .then(function (r) { return r.json(); })
      .then(function (files) {
        statusEl.textContent = files.length + ' 个文档';
        if (!currentPath) buildTree(files);
      })
      .catch(function (err) {
        statusEl.textContent = '加载失败: ' + err.message;
      });
  }
  loadDocs();
  setInterval(loadDocs, 30000);
})();
</script>
</body>
</html>
"""


def get_web_html() -> str:
    """Return the complete HTML page for mobile read-only browsing."""
    return _HTML
