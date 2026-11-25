# -*- coding: utf-8 -*-
"""冒烟：QQ/NapCat 下载完整性校验（verify_download + 已核实清单 + 降级链）。

全部离线：verify_download 是纯函数，用 tempfile 构造假文件逐场景断言；
zip-slip 场景用临时目录实测 qq_services.install_napcat_zip 的成员过滤；
另做两条源码检查（无空串哈希常量、下载点都传了 expect）。
不联网、不碰生产数据（QQBOT_DATA_DIR 指向临时目录）。"""
import hashlib
import os
import pathlib
import sys
import tempfile
import zipfile

TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="dlcheck-iso-"))
# 必须在 import 项目模块之前设置，让 DATA_DIR 落在临时目录
os.environ["QQBOT_DATA_DIR"] = str(TMP_ROOT / "data")

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_napcat_dl as ND          # noqa: E402
import qq_napcat_runtime as RT     # noqa: E402
import qq_services as S            # noqa: E402

fails = []
passed = 0


def chk(name, cond):
    global passed
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        passed += 1
    else:
        fails.append(name)


def make_file(rel, content):
    p = TMP_ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


ZIP_MAGIC = b"PK\x03\x04"
GOOD = ZIP_MAGIC + b"fake napcat shell payload" * 8
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()

# --- 场景1：哈希正确 → 通过 ---
ok, msg, det = ND.verify_download(
    str(make_file("ok.zip", GOOD)),
    {"sha256": GOOD_SHA, "size": len(GOOD), "magic": (ZIP_MAGIC,),
     "label": "napcat.zip"})
chk("场景1 哈希正确→通过(reason=ok,已做哈希)",
    ok and det.get("reason") == "ok" and det.get("hash_checked") is True)

# --- 场景2：哈希不对 → 拒绝，写明是哈希不对 ---
bad = GOOD + b"tampered"
ok, msg, det = ND.verify_download(
    str(make_file("badhash.zip", bad)),
    {"sha256": GOOD_SHA, "size": len(bad), "magic": (ZIP_MAGIC,)})
chk("场景2 哈希不对→拒绝且 msg 写明哈希",
    (not ok) and "哈希不对" in msg and det.get("reason") == "hash_mismatch")

# --- 场景2b：大小不符 → 拒绝 ---
ok, msg, det = ND.verify_download(
    str(make_file("badsize.zip", GOOD)),
    {"sha256": GOOD_SHA, "size": len(GOOD) + 1})
chk("场景2b 大小不符→拒绝且 msg 写明大小",
    (not ok) and "大小不符" in msg and det.get("reason") == "size_mismatch")

# --- 场景3：清单里没有的文件名 → 拒绝（返回 None 即拒绝依据） ---
chk("场景3 清单里没有→_qq_expect_for 返回 None",
    RT._qq_expect_for("QQ_9.9.99_999999_x64_99.exe") is None)
exp = RT._qq_expect_for(RT._QQ_EXE_NAME)
chk("场景3 清单命中→实测 size+sha256+PE 头齐备",
    bool(exp) and exp.get("size") == 312335528
    and exp.get("sha256") == ("685c989145381d9bc849948b836270111"
                              "a8b6b8cebf2068ff158ac9d2324bc44")
    and exp.get("magic") == (b"MZ",))
chk("场景3 清单文件名与 _QQ_EXE_NAME 一致",
    RT._QQ_EXE_NAME in RT._QQ_PACKAGE_MANIFEST)

# --- 场景5：文件头不是声明类型 → 拒绝 ---
ok, msg, det = ND.verify_download(
    str(make_file("wronghead.zip", b"MZ" + b"x" * 64)),
    {"sha256": "", "min_size": 16, "magic": (ZIP_MAGIC,),
     "allow_unsigned": True})
chk("场景5 文件头不符→拒绝且 msg 写明文件头",
    (not ok) and "文件头" in msg and det.get("reason") == "magic_mismatch")

# --- 场景6：无哈希+允许降级 → 大小下限+文件头通过，标明未做哈希校验 ---
ok, msg, det = ND.verify_download(
    str(make_file("legacy.zip", GOOD)),
    {"sha256": "", "min_size": 16, "magic": (ZIP_MAGIC,),
     "allow_unsigned": True})
chk("场景6 无哈希+允许降级→通过且标明未做哈希校验",
    ok and det.get("reason") == "ok_unsigned"
    and det.get("hash_checked") is False and "未做哈希校验" in msg)

# --- 场景6b：无哈希+不允许降级 → 拒绝（不许空串放行） ---
ok, msg, det = ND.verify_download(
    str(make_file("noallow.zip", GOOD)), {"sha256": ""})
chk("场景6b 无哈希+不允许降级→拒绝(unsigned_not_allowed)",
    (not ok) and det.get("reason") == "unsigned_not_allowed")

# --- 场景6c：降级路径大小低于下限 → 拒绝 ---
ok, msg, det = ND.verify_download(
    str(make_file("tiny.zip", b"PK\x03\x04tiny")),
    {"sha256": "", "min_size": 1 << 20, "magic": (ZIP_MAGIC,),
     "allow_unsigned": True})
chk("场景6c 降级路径低于大小下限→拒绝",
    (not ok) and det.get("reason") == "size_mismatch")

# --- 场景7：zip-slip——install_napcat_zip 对穿越成员的实际防御 ---
iso = TMP_ROOT / "zipslip"
qqnt = iso / "qqnt"
qqnt.mkdir(parents=True)
(qqnt / "QQ.exe").write_bytes(b"MZ fake qq")
zp = iso / "evil.zip"
with zipfile.ZipFile(zp, "w") as zf:
    zf.writestr("napcat.mjs", "// napcat")
    zf.writestr("launcher-user.bat", "@echo off\r\n")
    zf.writestr("../evil.dll", "EVIL")            # 父目录
    zf.writestr("..\\evil_back.dll", "EVIL")      # 反斜杠父目录
    zf.writestr("/abs_evil.dll", "EVIL")          # 绝对路径
    zf.writestr("C:\\evil_drive.dll", "EVIL")     # 盘符
ok_i, msg_i = S.install_napcat_zip({"qqnt_dir": str(qqnt)}, str(zp))
# 只查释放产物（排除测试输入 evil.zip 自身）：任何 evil*.dll 都不得出现
leaked = [str(p) for p in iso.rglob("*.dll") if "evil" in p.name.lower()]
chk("场景7 zip-slip 成员全部被拒、正常成员照常释放",
    ok_i and not leaked and (qqnt / "napcat.mjs").is_file())

# --- 场景8：源码检查——不再有「哈希为空串就跳过」分支 ---
scripts = pathlib.Path("plugins", "qq-bot", "scripts")
src_rt = (scripts / "qq_napcat_runtime.py").read_text(encoding="utf-8")
src_dl = (scripts / "qq_napcat_dl.py").read_text(encoding="utf-8")
chk("场景8 runtime 已无空串哈希常量 RUNTIME_QQ_HASH",
    "RUNTIME_QQ_HASH" not in src_rt)
chk("场景8 QQ 下载点传清单 expect（含缓存校验路径）",
    "expect=exp_qq" in src_rt and "ND.verify_download(str(qq_exe), exp_qq)" in src_rt)
chk("场景8 NapCat 下载点传 expect（直连与镜像共用）",
    src_rt.count("exp_shell") >= 5
    and "ND._download_public(url, zpath.name, st2, exp_shell)" in src_rt)
chk("场景8 dl.start 下载点传 expect（直连与镜像共用）",
    "st, exp)" in src_dl and "_download_public(url, \"napcat-shell-%s.zip\" % ver, st, exp)" in src_dl)
chk("场景8 verify_download 对空哈希默认拒绝（无显式 allow_unsigned 不放行）",
    (not ND.verify_download(str(make_file("dfl.zip", GOOD)),
                            {"sha256": ""})[0])
    and ND.verify_download(str(TMP_ROOT / "dfl.zip"),
                           {"sha256": "", "allow_unsigned": True})[0])

# --- digest 解析（GitHub API asset → 校验期望） ---
chk("digest sha256: 前缀解析正确",
    ND._asset_expect({"digest": "sha256:abc123", "size": 5})
    == {"sha256": "abc123", "size": 5})
chk("digest 缺失→空串（调用方走降级）",
    ND._asset_expect({"size": 5}) == {"sha256": "", "size": 5})
chk("digest 非 sha256 前缀→不采信",
    ND._asset_expect({"digest": "md5:abc", "size": 9})
    == {"sha256": "", "size": 9})

print("%d 项通过，%d 项失败" % (passed, len(fails)))
if not fails:
    # 全绿才清理自建的临时目录（精确路径；FAIL 时保留现场供排查）
    import shutil
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print("cleaned: %s" % TMP_ROOT)
sys.exit(1 if fails else 0)
