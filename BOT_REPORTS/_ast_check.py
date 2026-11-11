# -*- coding: utf-8 -*-
"""AST 验证：qq_daemon.py 所有 threading.Thread(args=...) 与目标函数签名参数数一致。"""
import ast

src = open("plugins/qq-bot/scripts/qq_daemon.py", encoding="utf-8").read()
tree = ast.parse(src)
defs = {f.name: [a.arg for a in f.args.args]
        for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
bad = 0
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        continue
    if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "threading"
            and node.func.attr == "Thread"):
        continue
    kw = {k.arg: k.value for k in node.keywords}
    tgt = kw.get("target")
    if tgt is None:
        continue
    fn = tgt.attr if isinstance(tgt, ast.Attribute) else (
        tgt.id if isinstance(tgt, ast.Name) else "?")
    argn = kw.get("args")
    nargs = len(argn.elts) if isinstance(argn, ast.Tuple) else (
        1 if argn is not None else 0)
    params = defs.get(fn)
    if params is None:
        print("?? %s: 函数定义未找到 (line %d)" % (fn, node.lineno))
        bad += 1
        continue
    expect = len(params) - 1 if params and params[0] == "self" else len(params)
    mark = "OK" if nargs == expect else "!!MISMATCH!!"
    if nargs != expect:
        bad += 1
    print("%s %s: args=%d def=%d (line %d)" % (mark, fn, nargs, expect, node.lineno))
print("BAD =", bad)
