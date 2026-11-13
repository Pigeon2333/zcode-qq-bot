# -*- coding: utf-8 -*-
"""提取 qq_admin_ui.py 内嵌 <script> 块整段 node --check（PAGE JS 规矩）。"""
import pathlib
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
src = pathlib.Path("plugins/qq-bot/scripts/qq_admin_ui.py").read_text(encoding="utf-8")
blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
print("script blocks:", len(blocks))
fails = 0
for i, b in enumerate(blocks):
    p = pathlib.Path("BOT_REPORTS/_ui_check_%d.js" % i)
    p.write_text(b, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
    status = "OK" if r.returncode == 0 else "FAIL"
    print(i, "lines=%d" % b.count("\n"), status)
    if r.returncode != 0:
        fails += 1
        print(r.stderr[:800])
    p.unlink(missing_ok=True)   # 检查完即删：留在仓库会被 Mimosa 扫出 SSRF 误报拦 commit
sys.exit(1 if fails else 0)
