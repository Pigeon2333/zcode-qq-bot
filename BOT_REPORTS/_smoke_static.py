# -*- coding: utf-8 -*-
"""静态门禁冒烟（纯 AST + 文本扫描；不执行被检代码、不联网、不写任何文件）。

拦这个项目反复出过的三类事故，外加两条配套检查：
  · 某个分支忘了给变量赋值（C）；
  · 线程实参个数和函数签名不一致（D）；
  · 新接口只写在 do_GET 里而前端用 POST 调（404 静默失败，E）。

检查范围：
  plugins/qq-bot/scripts/*.py
  plugins/qq-bot/scripts/plugins/*/plugin.py
  market/*/engine.py、market/*/plugin.py

规则与输出（输出行前缀 = 规则名，_run_all_smoke.py 只数 PASS/FAIL 行）：
  A-语法         每个文件 ast.parse，失败即 FAIL。
  B-未定义名     一个被读取（Load）的名字合法，当且仅当它是：内置名；模块级绑定
                 （import、def/class、赋值、for 目标、with…as、except…as、函数内
                 global 声明等）；本层或外层函数的参数/局部绑定/nonlocal；推导式
                 变量。按「在可达作用域里绑定过」判断，不做流敏感检查（先读后写
                 的流敏感问题归 C）。个别误报走下面 EXEMPT_B 豁免表
                 （文件 + 名字 + 原因），不做大范围关闭。
  C-兄弟分支赋值  同一函数同一语句块里，名字只在 if 的一侧（或 try 的 body、某个
                 except 分支）被赋值，随后（同层后续语句及其内部嵌套、嵌套函数里）
                 被读取，而它之前未被确定赋值、也不是参数/全局名/模块级名 → FAIL。
                 防误报的例外（都有静态依据）：缺失赋值的那一侧必然
                 return/raise/continue/break/sys.exit() 时不报（走到后面说明赋值
                 侧已执行）；try 的某个 except 整体必然终止时不再作候选边；try 的
                 首句是 import 且没有任何 except 会吞 ImportError 系时不报；后面
                 同样的 if 条件里读取、而该条件先前的分支以哨兵默认值初始化了它、
                 且条件变量未被中途改写时不报。
  D-线程传参     threading.Thread(target=f, args=(…), kwargs={…})：f 能静态解析到
                 （模块级函数 / self.方法 / cls.方法 / 类名.方法 / 已知实例的方法）
                 时，实参个数与签名核对（考虑默认参数、*args、**kwargs、kwonly、
                 self/cls 绑定）。解析不到的目标跳过。
  E-接口一致性   页面脚本（qq_admin_ui.py 内嵌 JS：api( / pwApi( / saveKV( /
                 saveKVLoad( / fetch( 的字面路径调用）与 qq_bot_agent.py 里
                 _ui_api("…", "POST", …) 的遥控调用，都打到 UiHandler
                 （qq_admin_ui.py）上，与其 do_GET / do_POST 里 u.path == "…"
                 或 u.path in (…) 的分支比对：GET 调的必须有 do_GET 分支、
                 POST 调的必须有 do_POST 分支，两边都没有也报。现存不一致只给
                 WARN（不计失败）；新回归会让 WARN 增加，人工看 WARN 行即可。

已知局限（有意为之，防误报优先）：
  * B 是「绑定过即可」，不查先读后写的顺序问题；del 之后的读取不查。
  * C 只看函数内的块，不跨块边界（如 for 体里赋值、循环外第一次读取不报）；
    读取位置的顺序在块内近似处理（先出现的赋值一律当作在读取之前，宁漏报）。
  * D 不解析 functools.partial、lambda、动态拼出的目标；args/kwargs 不是纯
    字面量时整条跳过。
  * E 只比对字面路径；路径含变量拼接且字面部分没有 '?' 的跳过；开屏预热数组
    tasks=[['/api/…'],…] 的 URL 不经过 api( 字面调用，不在比对范围。
  * 旧的 BOT_REPORTS/_ast_check.py（按写死行号找函数，已失效）由本脚本取代。

用法：PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 python BOT_REPORTS/_smoke_static.py
"""
import ast
import builtins
import os
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "plugins" / "qq-bot" / "scripts"
TRY_TYPES = (ast.Try, getattr(ast, "TryStar", ast.Try))

BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__spec__", "__package__", "__loader__",
    "__builtins__", "__debug__", "__annotations__", "__cached__",
    "__module__", "__qualname__", "__warningregistry__",
}

# B 规则豁免表：{(相对路径, 名字): 原因}。只收已证实的误报，附原因。
EXEMPT_B = {}


def collect_files():
    pats = [SRC.glob("*.py"),
            SRC.glob("plugins/*/plugin.py"),
            (ROOT / "market").glob("*/engine.py"),
            (ROOT / "market").glob("*/plugin.py")]
    seen, out = set(), []
    for g in pats:
        for p in sorted(g):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    return out


def rel(p):
    return os.path.relpath(str(p), str(ROOT)).replace("\\", "/")


# ---------- 通用 AST 小工具 ----------

def target_names(node):
    out = set()
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            out |= target_names(e)
    elif isinstance(node, ast.Starred):
        out |= target_names(node.value)
    return out


def param_names(a):
    out = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    if a.vararg:
        out.add(a.vararg.arg)
    if a.kwarg:
        out.add(a.kwarg.arg)
    return out


def comp_target_names(e):
    out = set()
    for gen in e.generators:
        out |= target_names(gen.target)
    return out


def collect_binds(stmts, class_as_local=False, comp_binds=True):
    """收集一组语句在本作用域里绑定的名字；不进嵌套函数/lambda 体。
    class_as_local=True 时把类体内的赋值也算进来（B 用，保守）；
    comp_binds=True 时把推导式目标也算进来（B 用，保守；C 的 all_assigned 传 False，
    因为推导式目标在块外并不可见）。"""
    binds, gdecl, nldecl = set(), set(), set()
    star = [False]

    def expr_walk(e):
        for child in ast.iter_child_nodes(e):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                binds.add(child.name)
                for d in child.decorator_list:
                    expr_walk(d)
                for d in child.args.defaults:
                    expr_walk(d)
                for d in child.args.kw_defaults:
                    if d:
                        expr_walk(d)
                continue
            if isinstance(child, ast.Lambda):
                for d in child.args.defaults:
                    expr_walk(d)
                for d in child.args.kw_defaults:
                    if d:
                        expr_walk(d)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.NamedExpr):
                binds.add(child.target.id)
                expr_walk(child.value)
                continue
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                if comp_binds:
                    binds.update(comp_target_names(child))
                expr_walk(child)
                continue
            expr_walk(child)

    def stmt_walk(s):
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            binds.add(s.name)
            for d in s.decorator_list:
                expr_walk(d)
            for d in s.args.defaults:
                expr_walk(d)
            for d in s.args.kw_defaults:
                if d:
                    expr_walk(d)
            return
        if isinstance(s, ast.ClassDef):
            binds.add(s.name)
            for d in s.decorator_list:
                expr_walk(d)
            for b in s.bases:
                expr_walk(b)
            for k in s.keywords:
                expr_walk(k.value)
            if class_as_local:
                for st in s.body:
                    stmt_walk(st)
            return
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            for al in s.names:
                if al.name == "*":
                    star[0] = True
                else:
                    binds.add(al.asname or al.name.split(".")[0])
            return
        if isinstance(s, ast.Assign):
            for t in s.targets:
                binds.update(target_names(t))
            expr_walk(s.value)
            return
        if isinstance(s, ast.AnnAssign):
            if isinstance(s.target, ast.Name):
                binds.add(s.target.id)
            if s.value is not None:
                expr_walk(s.value)
            return
        if isinstance(s, ast.AugAssign):
            if isinstance(s.target, ast.Name):
                binds.add(s.target.id)
            expr_walk(s.value)
            return
        if isinstance(s, (ast.Global, ast.Nonlocal)):
            (gdecl if isinstance(s, ast.Global) else nldecl).update(s.names)
            return
        if isinstance(s, (ast.For, getattr(ast, "AsyncFor", ast.For))):
            binds.update(target_names(s.target))
            expr_walk(s.iter)
            for st in s.body:
                stmt_walk(st)
            for st in s.orelse:
                stmt_walk(st)
            return
        if isinstance(s, ast.While):
            expr_walk(s.test)
            for st in s.body:
                stmt_walk(st)
            for st in s.orelse:
                stmt_walk(st)
            return
        if isinstance(s, ast.If):
            expr_walk(s.test)
            for st in s.body:
                stmt_walk(st)
            for st in s.orelse:
                stmt_walk(st)
            return
        if isinstance(s, (ast.With, getattr(ast, "AsyncWith", ast.With))):
            for it in s.items:
                expr_walk(it.context_expr)
                if it.optional_vars is not None:
                    binds.update(target_names(it.optional_vars))
            for st in s.body:
                stmt_walk(st)
            return
        if isinstance(s, TRY_TYPES):
            for st in s.body:
                stmt_walk(st)
            for h in s.handlers:
                if h.name:
                    binds.add(h.name)
                if h.type is not None:
                    expr_walk(h.type)
                for st in h.body:
                    stmt_walk(st)
            for st in s.orelse:
                stmt_walk(st)
            for st in s.finalbody:
                stmt_walk(st)
            return
        if isinstance(s, getattr(ast, "Match", ())):
            expr_walk(s.subject)
            for case in s.cases:
                for n in ast.walk(case.pattern):
                    if isinstance(n, getattr(ast, "MatchAs", ())) and n.name:
                        binds.add(n.name)
                    elif isinstance(n, getattr(ast, "MatchStar", ())) and n.name:
                        binds.add(n.name)
                    elif isinstance(n, getattr(ast, "MatchMapping", ())) and n.rest:
                        binds.add(n.rest)
                if case.guard is not None:
                    expr_walk(case.guard)
                for st in case.body:
                    stmt_walk(st)
            return
        expr_walk(s)

    for st in stmts:
        stmt_walk(st)
    return binds, gdecl, nldecl, star[0]


def stmt_exprs(s):
    """语句自身层级里的表达式（不含子语句块、不含嵌套 def/lambda 体）。"""
    if isinstance(s, ast.Expr):
        return [s.value]
    if isinstance(s, ast.Assign):
        return [s.value]
    if isinstance(s, ast.AnnAssign):
        return [x for x in (s.annotation, s.value) if x is not None]
    if isinstance(s, ast.AugAssign):
        return [s.value]
    if isinstance(s, ast.Return):
        return [s.value] if s.value is not None else []
    if isinstance(s, ast.Delete):
        return list(s.targets)
    if isinstance(s, ast.Raise):
        return [x for x in (s.exc, s.cause) if x is not None]
    if isinstance(s, ast.Assert):
        return [x for x in (s.test, s.msg) if x is not None]
    if isinstance(s, (ast.If, ast.While)):
        return [s.test]
    if isinstance(s, (ast.For, getattr(ast, "AsyncFor", ast.For))):
        return [s.iter]
    if isinstance(s, (ast.With, getattr(ast, "AsyncWith", ast.With))):
        return [it.context_expr for it in s.items]
    if isinstance(s, TRY_TYPES):
        return [h.type for h in s.handlers if h.type is not None]
    if isinstance(s, getattr(ast, "Match", ())):
        return [s.subject] + [c.guard for c in s.cases if c.guard is not None]
    if isinstance(s, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue,
                      ast.Import, ast.ImportFrom)):
        return []
    return [c for c in ast.iter_child_nodes(s) if isinstance(c, ast.expr)]


def stmt_sub_blocks(s):
    if isinstance(s, ast.If):
        return [s.body, s.orelse]
    if isinstance(s, (ast.For, getattr(ast, "AsyncFor", ast.For))):
        return [s.body, s.orelse]
    if isinstance(s, ast.While):
        return [s.body, s.orelse]
    if isinstance(s, (ast.With, getattr(ast, "AsyncWith", ast.With))):
        return [s.body]
    if isinstance(s, TRY_TYPES):
        return [s.body] + [h.body for h in s.handlers] + [s.orelse, s.finalbody]
    if isinstance(s, getattr(ast, "Match", ())):
        return [c.body for c in s.cases]
    return []


def match_capture(case):
    out = set()
    for n in ast.walk(case.pattern):
        if isinstance(n, getattr(ast, "MatchAs", ())) and n.name:
            out.add(n.name)
        elif isinstance(n, getattr(ast, "MatchStar", ())) and n.name:
            out.add(n.name)
        elif isinstance(n, getattr(ast, "MatchMapping", ())) and n.rest:
            out.add(n.rest)
    return out


def terminates(stmts):
    """这组语句是否必然不落到块尾（近似：出现直接的 return/raise/continue/break/
    sys.exit()/exit()，或 if 的两侧都必然终止）。宁可漏判（少抑制=多报），
    也不把可能落空的当必然终止。"""
    for s in stmts:
        if isinstance(s, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return True
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
            f = s.value.func
            if (isinstance(f, ast.Name) and f.id in ("exit", "quit")) or \
               (isinstance(f, ast.Attribute) and f.attr in ("exit", "_exit") and
               isinstance(f.value, ast.Name) and f.value.id in ("sys", "os")):
                return True
        if isinstance(s, ast.If) and s.orelse and terminates(s.body) and terminates(s.orelse):
            return True
    return False


def handler_catches_import(h):
    """except 子句会不会吞掉 import 抛的错（ImportError 系或宽泛类型）。"""
    t = h.type
    if t is None:
        return True
    names = []
    if isinstance(t, (ast.Tuple, getattr(ast, "List", ast.Tuple))):
        for x in t.elts:
            if isinstance(x, ast.Name):
                names.append(x.id)
            elif isinstance(x, ast.Attribute):
                names.append(x.attr)
    elif isinstance(t, ast.Name):
        names.append(t.id)
    elif isinstance(t, ast.Attribute):
        names.append(t.attr)
    else:
        return True                      # 看不懂的类型按会吞处理
    return any(n in ("Exception", "BaseException", "ImportError",
                     "ModuleNotFoundError") for n in names)


def stmt_all_assigned(s):
    return collect_binds([s], class_as_local=False, comp_binds=False)[0]


# ---------- B：未定义名 + C：兄弟分支赋值（共用一个作用域遍历） ----------

class Scope(object):
    def __init__(self, is_class, params=frozenset(), binds=frozenset(),
                 gdecl=frozenset(), nldecl=frozenset()):
        self.is_class = is_class
        self.params = params
        self.binds = binds
        self.gdecl = gdecl
        self.nldecl = nldecl


def rule_bc(relpath, tree, module_binds, module_star, b_issues, c_issues):
    b_reads = [0]

    def read_ok(n, ctx):
        for sc in ctx:
            if n in sc.binds or n in sc.params or n in sc.gdecl or n in sc.nldecl:
                return True
        if module_star or n in module_binds or n in BUILTINS:
            return True
        return (relpath, n) in EXEMPT_B

    def visit_expr(e, ctx):
        if isinstance(e, ast.Lambda):
            for d in e.args.defaults:
                visit_expr(d, ctx)
            for d in e.args.kw_defaults:
                if d is not None:
                    visit_expr(d, ctx)
            sc = Scope(False, param_names(e.args))
            visit_expr(e.body, [x for x in ctx if not x.is_class] + [sc])
            return
        if isinstance(e, ast.Name):
            if isinstance(e.ctx, ast.Load):
                b_reads[0] += 1
                if not read_ok(e.id, ctx):
                    b_issues.append((relpath, e.lineno, e.id))
            return
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            seen = set()
            for k, gen in enumerate(e.generators):
                # 首个生成器的迭代式在外层作用域求值，其余能看到前面的目标
                visit_expr(gen.iter, ctx if k == 0 else ctx + [Scope(False, set(seen))])
                seen |= target_names(gen.target)
                for c in gen.ifs:
                    visit_expr(c, ctx + [Scope(False, set(seen))])
            inner = Scope(False, set(seen))
            if isinstance(e, ast.DictComp):
                visit_expr(e.key, ctx + [inner])
                visit_expr(e.value, ctx + [inner])
            else:
                visit_expr(e.elt, ctx + [inner])
            return
        for child in ast.iter_child_nodes(e):
            if isinstance(child, ast.expr):
                visit_expr(child, ctx)

    def visit_args(a, ctx):
        for d in a.defaults:
            visit_expr(d, ctx)
        for d in a.kw_defaults:
            if d is not None:
                visit_expr(d, ctx)
        for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            if x is not None and x.annotation is not None:
                visit_expr(x.annotation, ctx)

    def visit_stmts(stmts, ctx):
        for s in stmts:
            visit_stmt(s, ctx)

    def visit_stmt(s, ctx):
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in s.decorator_list:
                visit_expr(d, ctx)
            visit_args(s.args, ctx)
            if s.returns is not None:
                visit_expr(s.returns, ctx)
            binds, g, nl, _ = collect_binds(s.body, class_as_local=True)
            sc = Scope(False, param_names(s.args), binds, g, nl)
            sc.parent_funcs = tuple(x for x in ctx if not x.is_class)
            # ---- C：以本函数为入口跑块分析 ----
            entry = set(sc.params) | set(sc.gdecl) | set(sc.nldecl)
            for p in sc.parent_funcs:
                entry |= p.params
            c_block(s.body, entry, s.name)
            # 方法体看不到类体绑定（类作用域只对类体内直接的读可见）
            visit_stmts(s.body, [x for x in ctx if not x.is_class] + [sc])
            return
        if isinstance(s, ast.ClassDef):
            for d in s.decorator_list:
                visit_expr(d, ctx)
            for b in s.bases:
                visit_expr(b, ctx)
            for k in s.keywords:
                visit_expr(k.value, ctx)
            cb, _, _, _ = collect_binds(s.body, class_as_local=True)
            ci = Scope(True, set(), cb)
            visit_stmts(s.body, ctx + [ci])
            return
        for e in stmt_exprs(s):
            visit_expr(e, ctx)
        for sub in stmt_sub_blocks(s):
            visit_stmts(sub, ctx)

    # ===== C 的数据流 =====

    def all_assigned(stmts):
        return collect_binds(stmts, class_as_local=False, comp_binds=False)[0]

    def block_definite(stmts, entry):
        d = set(entry)
        for s in stmts:
            d = definite_after(s, d)
        return d

    def branch_defs(stmts, entry):
        return block_definite(stmts, entry) - entry

    def definite_after(s, d):
        if isinstance(s, ast.Assign):
            out = set(d)
            for t in s.targets:
                out |= target_names(t)
            return out
        if isinstance(s, ast.AnnAssign):
            return d | (target_names(s.target) if isinstance(s.target, ast.Name) else set())
        if isinstance(s, ast.AugAssign):
            return d | (target_names(s.target) if isinstance(s.target, ast.Name) else set())
        if isinstance(s, ast.NamedExpr):
            return d | target_names(s.target)
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            out = set(d)
            for al in s.names:
                if al.name != "*":
                    out.add(al.asname or al.name.split(".")[0])
            return out
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return d | {s.name}
        if isinstance(s, ast.If):
            bd = branch_defs(s.body, d)
            if not s.orelse:
                return d
            ed = branch_defs(s.orelse, d)
            if terminates(s.body):
                return d | ed
            if terminates(s.orelse):
                return d | bd
            return d | (bd & ed)
        if isinstance(s, TRY_TYPES):
            paths = [branch_defs(s.body, d) | (branch_defs(s.orelse, d) if s.orelse else set())]
            for h in s.handlers:
                if not terminates(h.body):       # 必然终止的分支到不了 try 之后
                    paths.append(branch_defs(h.body, d))
            inter = set(paths[0])
            for p in paths[1:]:
                inter &= p
            out = d | inter
            for st in s.finalbody:
                out = definite_after(st, out)
            return out
        if isinstance(s, (ast.For, getattr(ast, "AsyncFor", ast.For))):
            out = d | target_names(s.target)
            if s.orelse:
                out |= branch_defs(s.orelse, d)
            return out
        if isinstance(s, ast.While):
            out = set(d)
            if s.orelse:
                out |= branch_defs(s.orelse, d)
            if isinstance(s.test, ast.Constant) and bool(s.test.value):
                out |= branch_defs(s.body, d)
            return out
        if isinstance(s, (ast.With, getattr(ast, "AsyncWith", ast.With))):
            names = set()
            for it in s.items:
                if it.optional_vars is not None:
                    names |= target_names(it.optional_vars)
            return d | names | branch_defs(s.body, d | names)
        return d

    def expr_read(name, e, d):
        """表达式里找 name 的首次读取行号；跳过嵌套函数体；推导式目标、walrus
        先绑定后读的不算。"""
        if isinstance(e, ast.Name):
            if e.id == name and isinstance(e.ctx, ast.Load) and name not in d:
                return e.lineno
            return None
        if isinstance(e, ast.Lambda):
            return expr_read(name, e.body, d | param_names(e.args))
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            dd = set(d) | comp_target_names(e)
            for gen in e.generators:
                r = expr_read(name, gen.iter, d)
                if r:
                    return r
                for c in gen.ifs:
                    r = expr_read(name, c, dd)
                    if r:
                        return r
            if isinstance(e, ast.DictComp):
                return (expr_read(name, e.key, dd) or expr_read(name, e.value, dd))
            return expr_read(name, e.elt, dd)
        if isinstance(e, ast.NamedExpr):
            r = expr_read(name, e.value, d)
            if r:
                return r
            return None if name == e.target.id else expr_read(name, e.target, d | {e.target.id})
        for child in ast.iter_child_nodes(e):
            if isinstance(child, ast.expr):
                r = expr_read(name, child, d)
                if r:
                    return r
        return None

    def stmt_read(name, s, d, guards=None):
        """单条语句里找 name 的首次读取（含嵌套块与嵌套函数体）。
        guards：同层先前的 if 测试文本 -> 该分支走完必然绑定的名字集合
        （同条件再入的分支里这些名字必然已绑定；None 表示测试变量在两点之间
        被赋值污染过，不再可用）。"""
        for e in stmt_exprs(s):
            r = expr_read(name, e, d)
            if r:
                return r
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for e in s.decorator_list:
                r = expr_read(name, e, d)
                if r:
                    return r
            for e in list(s.args.defaults) + [x for x in s.args.kw_defaults if x is not None]:
                r = expr_read(name, e, d)
                if r:
                    return r
            dd = d | param_names(s.args) | all_assigned(s.body)
            for st in s.body:
                r = stmt_read(name, st, dd, guards)
                if r:
                    return r
            return None
        if isinstance(s, ast.ClassDef):
            dd = d | all_assigned(s.body)
            for st in s.body:
                r = stmt_read(name, st, dd, guards)
                if r:
                    return r
            return None
        if isinstance(s, ast.If):
            gset = (guards or {}).get(ast.unparse(s.test)) or set()
            for sub, extra in ((s.body, all_assigned(s.body) | gset),
                               (s.orelse, all_assigned(s.orelse))):
                dd = d | extra
                for st in sub:
                    r = stmt_read(name, st, dd, guards)
                    if r:
                        return r
            return None
        if isinstance(s, TRY_TYPES):
            groups = [(s.body, all_assigned(s.body))]
            for h in s.handlers:
                # except 自己 as 的名字在本分支内可见，先加进 definite 再找读
                extra = all_assigned(h.body) | ({h.name} if h.name else set())
                groups.append((h.body, extra))
            groups.append((s.orelse, all_assigned(s.orelse)))
            groups.append((s.finalbody, all_assigned(s.finalbody)))
            for sub, extra in groups:
                dd = d | extra
                for st in sub:
                    r = stmt_read(name, st, dd, guards)
                    if r:
                        return r
            return None
        if isinstance(s, (ast.For, getattr(ast, "AsyncFor", ast.For))):
            extra = target_names(s.target) | all_assigned(s.body)
            for st in s.body:
                r = stmt_read(name, st, d | extra)
                if r:
                    return r
            for st in s.orelse:
                r = stmt_read(name, st, d | target_names(s.target) | all_assigned(s.orelse))
                if r:
                    return r
            return None
        if isinstance(s, ast.While):
            extra = all_assigned(s.body)
            for st in s.body:
                r = stmt_read(name, st, d | extra)
                if r:
                    return r
            for st in s.orelse:
                r = stmt_read(name, st, d | all_assigned(s.orelse))
                if r:
                    return r
            return None
        if isinstance(s, (ast.With, getattr(ast, "AsyncWith", ast.With))):
            names = set()
            for it in s.items:
                if it.optional_vars is not None:
                    names |= target_names(it.optional_vars)
            extra = names | all_assigned(s.body)
            for st in s.body:
                r = stmt_read(name, st, d | extra)
                if r:
                    return r
            return None
        if isinstance(s, getattr(ast, "Match", ())):
            for case in s.cases:
                dd = d | match_capture(case) | all_assigned(case.body)
                for st in case.body:
                    r = stmt_read(name, st, dd, guards)
                    if r:
                        return r
            return None
        return None

    def first_read(name, stmts, entry, cand_idx):
        """从块头重放 definite 与「同条件哨兵」表，从 cand_idx+1 起找读取。
        候选语句自身的 if 条件也进表：后面同条件的分支里，该分支走完
        必然绑定的名字一定已经绑定（条件变量未被中途改写时）。"""
        guards, guard_vars = {}, {}
        d = set(entry)
        for k, s in enumerate(stmts):
            if k > cand_idx:
                r = stmt_read(name, s, d, guards)
                if r:
                    return r
            est_t = None
            if isinstance(s, ast.If):
                est_t = ast.unparse(s.test)
                guards[est_t] = branch_defs(s.body, d)
                guard_vars[est_t] = {x.id for x in ast.walk(s.test) if isinstance(x, ast.Name)}
            asg_full = stmt_all_assigned(s)
            # 建立 guard 的那条 if：body 里的赋值只在 body 跑过的前提下发生，
            # 不破坏「后面同条件 ⇒ 前面分支跑过」；只有 orelse 的赋值算污染
            asg_est = (collect_binds(s.orelse, class_as_local=False, comp_binds=False)[0]
                       if isinstance(s, ast.If) and s.orelse else set())
            for t in list(guards):
                if guards[t] is None:
                    continue
                asg = asg_est if t == est_t else asg_full
                if guard_vars[t] & asg:
                    guards[t] = None
            d = definite_after(s, d)
        return None

    def c_block(stmts, definite, funcname):
        d = set(definite)
        for i, s in enumerate(stmts):
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue                      # 嵌套函数自己作为入口跑（visit_stmt）
            if isinstance(s, ast.ClassDef):
                cb, _, _, _ = collect_binds(s.body, class_as_local=True)
                c_block(s.body, d | cb, funcname)
            if isinstance(s, ast.If):
                ua_b, ua_e = all_assigned(s.body), all_assigned(s.orelse)
                df_b, df_e = branch_defs(s.body, d), branch_defs(s.orelse, d)
                cand = (ua_b ^ ua_e) | (df_b ^ df_e)
                if s.orelse and terminates(s.orelse):
                    cand -= {n for n in cand if n not in ua_e and n not in df_e}
                if terminates(s.body) and s.orelse:
                    cand -= {n for n in cand if n not in ua_b and n not in df_b}
                c_report(cand, s, stmts, i, d, funcname, definite)
                c_block(s.body, block_definite(s.body, d), funcname)
                c_block(s.orelse, block_definite(s.orelse, d), funcname)
            elif isinstance(s, TRY_TYPES):
                # 只有「可能落回 try 之后」的分支算候选边：必然终止的分支
                # （整个分支 return/raise/continue/break/sys.exit）到不了后面
                live = [s.body] + [h.body for h in s.handlers if not terminates(h.body)]
                body_df = branch_defs(s.body, d) | (branch_defs(s.orelse, d) if s.orelse else set())
                sides_ua = [all_assigned(x) for x in live]
                sides_df = [body_df] + [branch_defs(x, d) for x in live[1:]]
                if len(live) == 1:
                    # 只剩 body 一条路到得了 try 之后：body 走完即确定，
                    # 候选只剩 body 里的条件赋值
                    cand = (sides_ua[0] | sides_df[0]) - sides_df[0]
                else:
                    present_ua, common_ua = set(), None
                    for ua in sides_ua:
                        present_ua |= ua
                        common_ua = ua if common_ua is None else (common_ua & ua)
                    present_df, common_df = set(), None
                    for df in sides_df:
                        present_df |= df
                        common_df = df if common_df is None else (common_df & df)
                    cand = (present_ua - common_ua) | (present_df - common_df)
                # try 首句是 import、且没有任何 except 会吞 ImportError 系：
                # 要么 import 成功，要么异常直接抛出（到不了 try 之后）→ 必然已绑定
                first = s.body[0] if s.body else None
                if isinstance(first, (ast.Import, ast.ImportFrom)) and \
                        not any(handler_catches_import(h) for h in s.handlers):
                    cand -= collect_binds([first], class_as_local=False, comp_binds=False)[0]
                c_report(cand, s, stmts, i, d, funcname, definite)
                c_block(s.body, block_definite(s.body, d), funcname)
                for h in s.handlers:
                    c_block(h.body, block_definite(h.body, d), funcname)
                c_block(s.orelse, block_definite(s.orelse, d), funcname)
                c_block(s.finalbody, d, funcname)
            elif isinstance(s, (ast.For, getattr(ast, "AsyncFor", ast.For))):
                c_block(s.body, block_definite(s.body, d | target_names(s.target)), funcname)
                c_block(s.orelse, block_definite(s.orelse, d | target_names(s.target)), funcname)
            elif isinstance(s, ast.While):
                c_block(s.body, block_definite(s.body, d), funcname)
                c_block(s.orelse, block_definite(s.orelse, d), funcname)
            elif isinstance(s, (ast.With, getattr(ast, "AsyncWith", ast.With))):
                names = set()
                for it in s.items:
                    if it.optional_vars is not None:
                        names |= target_names(it.optional_vars)
                c_block(s.body, block_definite(s.body, d | names), funcname)
            elif isinstance(s, getattr(ast, "Match", ())):
                for case in s.cases:
                    c_block(case.body, d | match_capture(case), funcname)
            d = definite_after(s, d)

    def c_report(cand, s, stmts, i, d, funcname, entry):
        for n in sorted(cand):
            if n in d or n in BUILTINS or n in module_binds or module_star:
                continue
            if (relpath, n) in EXEMPT_B:
                continue
            hit = first_read(n, stmts, entry, i)
            if hit is not None:
                c_issues.append((relpath, s.lineno, n, funcname, hit))

    visit_stmts(tree.body, [])
    return b_reads[0]


def module_scope(tree):
    binds, g, nl, star = collect_binds(tree.body, class_as_local=False, comp_binds=True)
    for n in ast.walk(tree):        # 函数里 global 声明的名字也视作模块级绑定
        if isinstance(n, ast.Global):
            binds |= set(n.names)
    return binds, star


# ---------- D：线程传参 ----------

def rule_d(relpath, tree, issues):
    mfuncs, mclasses = {}, {}

    def scan_names(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mfuncs.setdefault(s.name, s)
            elif isinstance(s, ast.ClassDef):
                mclasses.setdefault(s.name, s)
            for sub in stmt_sub_blocks(s):
                scan_names(sub)

    scan_names(tree.body)

    aliases = {}

    def scan_assigns(stmts):
        for s in stmts:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                v = s.value
                if isinstance(v, ast.Name):
                    aliases[s.targets[0].id] = ("ref", v.id)
                elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in mclasses:
                    aliases[s.targets[0].id] = ("inst", v.func.id)
            for sub in stmt_sub_blocks(s):
                scan_assigns(sub)

    scan_assigns(tree.body)

    def resolve_alias(n):
        seen = set()
        while n and n not in seen:
            seen.add(n)
            ent = aliases.get(n)
            if ent is None:
                return None
            if ent[0] == "inst":
                return ent
            n = ent[1]
        return None

    mods, froms = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                mods[al.asname or al.name.split(".")[0]] = al.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for al in node.names:
                if al.name != "*":
                    froms[al.asname or al.name] = (node.module, al.name)

    def deco_has(fd, nm):
        for x in fd.decorator_list:
            if (isinstance(x, ast.Name) and x.id == nm) or \
               (isinstance(x, ast.Attribute) and x.attr == nm):
                return True
        return False

    def method_of(cls_node, name, depth=0):
        for st in cls_node.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == name:
                return st
        if depth < 5:
            for b in cls_node.bases:
                if isinstance(b, ast.Name) and b.id in mclasses:
                    r = method_of(mclasses[b.id], name, depth + 1)
                    if r:
                        return r
        return None

    def resolve_target(t, cur_cls):
        """返回 (函数定义, 前缀参数个数)；解析不到返回 (None, 0)。"""
        if isinstance(t, ast.Name):
            n = t.id
            if n in mfuncs:
                return mfuncs[n], 0
            return None, 0                    # 类名/未知名跳过
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
            base, attr = t.value.id, t.attr
            if base in ("self", "cls") and cur_cls is not None:
                m = method_of(cur_cls, attr)
                if m is not None:
                    return m, (0 if deco_has(m, "staticmethod") else 1)
                return None, 0
            if base in mclasses:
                m = method_of(mclasses[base], attr)
                if m is not None:
                    if deco_has(m, "staticmethod"):
                        return m, 0
                    if deco_has(m, "classmethod"):
                        return m, 1
                    return m, 0               # 未绑定方法：self 得显式传
                return None, 0
            r = resolve_alias(base)
            if r and r[0] == "inst" and r[1] in mclasses:
                m = method_of(mclasses[r[1]], attr)
                if m is not None:
                    return m, (0 if deco_has(m, "staticmethod") else 1)
        return None, 0

    def sig_str(fd, prefix):
        a = fd.args
        parts = [x.arg for x in (a.posonlyargs + a.args)][prefix:]
        if a.vararg:
            parts.append("*" + a.vararg.arg)
        elif a.kwonlyargs:
            parts.append("*")
        parts += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            parts.append("**" + a.kwarg.arg)
        return "(" + ", ".join(parts) + ")"

    def kw_of(call, name, idx):
        for k in call.keywords:
            if k.arg == name:
                return k.value
        if len(call.args) > idx:
            return call.args[idx]
        return None

    def is_thread_call(call):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "Thread" and \
                isinstance(f.value, ast.Name) and mods.get(f.value.id) == "threading":
            return True
        if isinstance(f, ast.Name) and froms.get(f.id) == ("threading", "Thread"):
            return True
        return False

    checked = [0]

    def handle(call, cur_cls):
        tgt = kw_of(call, "target", 1)
        if tgt is None:
            return
        fd, prefix = resolve_target(tgt, cur_cls)
        if fd is None:
            return
        a = fd.args
        args_v = kw_of(call, "args", 3)
        kw_v = kw_of(call, "kwargs", 4)
        if args_v is not None and not isinstance(args_v, ast.Tuple):
            if not (isinstance(args_v, ast.Constant) and args_v.value is None):
                return
        nargs = len(args_v.elts) if isinstance(args_v, ast.Tuple) else 0
        if kw_v is not None and not isinstance(kw_v, ast.Dict):
            return
        kwset = set()
        if isinstance(kw_v, ast.Dict):
            for k in kw_v.keys:
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    return
                kwset.add(k.value)
        checked[0] += 1
        orig = [x.arg for x in (a.posonlyargs + a.args)]
        ndef = len(a.defaults)
        required_after = [x for x in orig[:len(orig) - ndef]][prefix:]
        pos_after = orig[prefix:]
        idx = {nm: i for i, nm in enumerate(pos_after)}
        posonly = {x.arg for x in a.posonlyargs}
        kwonly = [x.arg for x in a.kwonlyargs]
        req_kw = [x.arg for x, dd in zip(a.kwonlyargs, a.kw_defaults) if dd is None]
        has_var = a.vararg is not None
        has_kw = a.kwarg is not None

        why = []
        known = set(pos_after) | set(kwonly)
        unknown = kwset - known
        if unknown and not has_kw:
            why.append("未知参数名 %s" % ",".join(sorted(unknown)))
        for k in sorted(kwset & posonly):
            why.append("%s 是 positional-only 参数却用关键字传" % k)
        dup = [k for k in kwset & set(pos_after) - posonly if idx[k] < nargs]
        if dup:
            why.append("参数 %s 既在 args 里又进了 kwargs" % ",".join(sorted(dup)))
        if nargs > len(pos_after) and not has_var:
            why.append("args 有 %d 个，签名最多收 %d 个位置参数" % (nargs, len(pos_after)))
        missing = [r for r in required_after
                   if not (idx.get(r, 1 << 30) < nargs or r in kwset)]
        if missing:
            why.append("缺必填参数 %s" % ",".join(missing))
        miss_kw = [r for r in req_kw if r not in kwset]
        if miss_kw:
            why.append("缺必填关键字参数 %s" % ",".join(sorted(miss_kw)))
        if why:
            issues.append((relpath, call.lineno, ast.unparse(tgt)[:60], nargs,
                           ",".join(sorted(kwset)) or "-", why, sig_str(fd, prefix)))

    def scan_expr(e, cur_cls):
        stack = [e]
        while stack:
            x = stack.pop()
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(x, ast.Call) and is_thread_call(x):
                handle(x, cur_cls)
            stack.extend(ast.iter_child_nodes(x))

    def walk_d(stmts, cur_cls):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_d(s.body, cur_cls)
                continue
            if isinstance(s, ast.ClassDef):
                walk_d(s.body, s)
                continue
            for e in stmt_exprs(s):
                scan_expr(e, cur_cls)
            for sub in stmt_sub_blocks(s):
                walk_d(sub, cur_cls)

    walk_d(tree.body, None)
    return checked[0]


# ---------- E：接口方法一致性 ----------

JS_CALL_RE = re.compile(r"\b(saveKVLoad|saveKV|pwApi|api|fetch)\s*\(")


def read_js_string(text, i):
    q = text[i]
    j = i + 1
    buf = []
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return "".join(buf), j + 1
        buf.append(c)
        j += 1
    return None, i


def read_balanced(text, i):
    """text[i] 是 '('，返回括号内文本（字符串整体替换成占位，防止里面的括号干扰）。"""
    depth = 0
    j = i
    buf = []
    while j < len(text):
        c = text[j]
        if c in ("'", '"', "`"):
            _, j2 = read_js_string(text, j)
            if j2 == j:
                buf.append(c)
                j += 1
                continue
            buf.append(text[j:j2])      # 原样保留，method:'POST' 才能被搜到
            j = j2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return "".join(buf)
        buf.append(c)
        j += 1
    return "".join(buf)


def frontend_js_calls(text):
    out = []
    for m in JS_CALL_RE.finditer(text):
        name = m.group(1)
        i = m.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] not in ("'", '"'):
            continue
        lit, _ = read_js_string(text, i)
        if lit is None:
            continue
        rest = text[_:].lstrip()
        if rest.startswith("+"):
            if "?" not in lit:
                continue                  # 变量拼在路径里 → 跳过
            path = lit.split("?", 1)[0]
        else:
            path = lit.split("?", 1)[0]
        method = "GET"
        if name in ("saveKV", "saveKVLoad"):
            method = "POST"
        elif name in ("api", "fetch"):
            argtxt = read_balanced(text, m.end() - 1)
            mm = re.search(r"method\s*:\s*['\"]([A-Za-z]+)['\"]", argtxt)
            if mm:
                method = mm.group(1).upper()
        if path.startswith("/api/"):
            line = text.count("\n", 0, m.start()) + 1
            out.append((line, path, method, "JS " + name))
    return out


def ui_api_calls(tree):
    """qq_bot_agent.py 里 _ui_api("/api/…", "POST", …) —— Python 侧的管理台调用。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id == "_ui_api" and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            path = node.args[0].value.split("?", 1)[0]
            method = "GET"
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and \
                    isinstance(node.args[1].value, str):
                method = node.args[1].value.upper()
            for kw in node.keywords:
                if kw.arg == "method" and isinstance(kw.value, ast.Constant) and \
                        isinstance(kw.value.value, str):
                    method = kw.value.value.upper()
            if path.startswith("/api/"):
                out.append((node.lineno, path, method, "_ui_api"))
    return out


def backend_routes(tree):
    get_r, post_r = set(), set()

    def is_path_expr(x):
        return isinstance(x, ast.Attribute) and x.attr == "path"

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            target = get_r if "GET" in node.name else (post_r if "POST" in node.name else None)
            if target is None:
                continue
            for c in ast.walk(node):
                if isinstance(c, ast.Compare) and len(c.ops) == 1 and \
                        isinstance(c.ops[0], (ast.Eq, ast.In)):
                    parts = [c.left] + list(c.comparators)
                    if any(is_path_expr(x) for x in parts):
                        for x in parts:
                            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                                target.add(x.value)
                            elif isinstance(x, (ast.Tuple, ast.List)):
                                for e in x.elts:
                                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                        target.add(e.value)
    return get_r, post_r


def main():
    files = collect_files()
    fails = []
    warns = []

    def fail(msg):
        line = "FAIL " + msg
        print(line)
        fails.append(line)

    def warn(msg):
        line = "WARN " + msg
        print(line)
        warns.append(line)

    # ---- A：语法 ----
    trees = {}
    a_bad = []
    for p in files:
        rp = rel(p)
        try:
            trees[rp] = ast.parse(p.read_bytes(), filename=str(p))
        except SyntaxError as e:
            a_bad.append((rp, e.lineno or 0, str(e).strip().replace("\n", " ")[:160]))
        except (OSError, ValueError) as e:
            a_bad.append((rp, 0, "读取失败: %r" % (e,)))
    for rp, ln, msg in a_bad:
        fail("A-语法 %s:%s — %s" % (rp, ln, msg))
    if not a_bad:
        print("PASS A-语法：%d 个文件 ast.parse 全部通过" % len(trees))

    # ---- B + C ----
    b_all, c_all = [], []
    n_reads = 0
    for rp in sorted(trees):
        tree = trees[rp]
        mb, mstar = module_scope(tree)
        b_issues, c_issues = [], []
        n_reads += rule_bc(rp, tree, mb, mstar, b_issues, c_issues)
        b_all.extend(b_issues)
        c_all.extend(c_issues)
    for rp, ln, n in sorted(b_all):
        fail("B-未定义名 %s:%d 名字 %s — 在可达作用域里找不到绑定" % (rp, ln, n))
    for rp, ln, n, fn, hit in sorted(c_all):
        fail("C-兄弟分支赋值 %s:%d 名字 %s — 函数 %s 里只在分支一侧赋值，"
             "第 %d 行读取时可能未绑定" % (rp, ln, n, fn, hit))
    if not b_all:
        print("PASS B-未定义名：%d 个文件共 %d 处名字读取全部可解析（豁免表 %d 条）"
              % (len(trees), n_reads, len(EXEMPT_B)))
    if not c_all:
        print("PASS C-兄弟分支赋值：全部函数块分析通过，0 处问题")
    else:
        print("FAIL C-兄弟分支赋值：共 %d 处" % len(c_all))

    # ---- D ----
    d_all = []
    n_thread = 0
    for rp in sorted(trees):
        issues = []
        n_thread += rule_d(rp, trees[rp], issues)
        d_all.extend(issues)
    for rp, ln, tgt, nargs, kws, why, sig in sorted(d_all):
        fail("D-线程传参 %s:%d target=%s args=%d kwargs=[%s] 签名%s — %s"
             % (rp, ln, tgt, nargs, kws, sig, "；".join(why)))
    if not d_all:
        print("PASS D-线程传参：可解析的 %d 处 threading.Thread 传参与签名全部一致" % n_thread)
    else:
        print("FAIL D-线程传参：共 %d 处" % len(d_all))

    # ---- E ----
    ui_rel = rel(SRC / "qq_admin_ui.py")
    agent_rel = rel(SRC / "qq_bot_agent.py")
    if ui_rel in trees and agent_rel in trees:
        get_r, post_r = backend_routes(trees[ui_rel])
        calls = frontend_js_calls((SRC / "qq_admin_ui.py").read_text(encoding="utf-8", errors="replace"))
        calls += ui_api_calls(trees[agent_rel])
        seen = {}
        for ln, path, method, src in calls:
            key = (path, method)
            seen.setdefault(key, (ln, src, 0))
            seen[key] = (seen[key][0], seen[key][1], seen[key][2] + 1)
        e_bad = []
        for (path, method), (ln, src, cnt) in sorted(seen.items()):
            if method == "GET" and path in get_r:
                continue
            if method == "POST" and path in post_r:
                continue
            has_get, has_post = path in get_r, path in post_r
            if has_get and not has_post:
                why = "do_POST 无分支（只有 do_GET）——POST 会 404"
            elif has_post and not has_get:
                why = "do_GET 无分支（只有 do_POST）——GET 会 404"
            else:
                why = "do_GET/do_POST 都没有分支——404"
            e_bad.append((path, method, ln, src, cnt, why))
        for path, method, ln, src, cnt, why in e_bad:
            warn("E-接口 %s:%d %s 调 %s（%d 处）— %s" % (ui_rel if src.startswith("JS") else agent_rel,
                                                        ln, src, method + " " + path, cnt, why))
        n_get = sum(1 for c in calls if c[2] == "GET")
        print("PASS E-接口一致性：前端调用 %d 处（GET %d / POST %d），"
              "UiHandler 分支 do_GET %d 个 / do_POST %d 个；WARN %d 条"
              % (len(calls), n_get, len(calls) - n_get, len(get_r), len(post_r), len(e_bad)))
    else:
        print("PASS E-接口一致性：qq_admin_ui.py / qq_bot_agent.py 语法不过，无法比对（见 A 规则）")

    print()
    print("汇总：%d 个文件；FAIL %d 条；WARN %d 条（E 规则现存不一致，不计失败）"
          % (len(files), len(fails), len(warns)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
