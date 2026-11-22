"""沉浸式壳重构（immersive-shell 分支）行为钉。

零 Vue 纯函数模块走 node 行为钉（仿 test_web_ui_audit.py 的 node 段）；
组件接线走静态文本钉。计划：docs/superpowers/plans/2026-11-22-immersive-shell.md
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

TPL = pathlib.Path(__file__).resolve().parent.parent / "server" / "templates"


def _run_node(js: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)


def test_shell_state_pure():
    # drawerNext：target===cur → null（再点即关），否则返回 target；
    # escTarget：settings > fan > drawer > dropdown；
    # loadSlots：畸形/缺键/越界 right 全回默认 {tree:false,right:null}。
    mod = (TPL / "js/utils/shellState.js").as_uri()
    js = (
        "import(%r).then(m=>{"
        "const dq=(a,b,w)=>{if(a!==b){console.error(w);process.exit(1)}};"
        "dq(m.drawerNext('file','file'),null,'drawerNext same');"
        "dq(m.drawerNext('file','search'),'search','drawerNext switch');"
        "dq(m.drawerNext(null,'monitor'),'monitor','drawerNext from null');"
        "dq(m.escTarget({settingsOpen:true,fanOpen:true,drawer:'file',dropdownOpen:true}),'settings','esc settings');"
        "dq(m.escTarget({fanOpen:true,drawer:'file',dropdownOpen:true}),'fan','esc fan');"
        "dq(m.escTarget({drawer:'file'}),'drawer','esc drawer');"
        "dq(m.escTarget({}),null,'esc none');"
        "const dflt=o=>o.tree===false&&o.right===null;"
        "dq(dflt(m.loadSlots(null)),true,'loadSlots null');"
        "dq(dflt(m.loadSlots('{bad json')),true,'loadSlots bad');"
        "const ok=m.loadSlots('{\"tree\":true,\"right\":\"ai\"}');"
        "dq(ok.tree===true&&ok.right==='ai',true,'loadSlots valid');"
        "dq(m.loadSlots('{\"tree\":true,\"right\":\"bogus\"}').right,null,'loadSlots bogus right');"
        "process.exit(0)})") % mod
    _run_node(js)
