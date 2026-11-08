# -*- coding: utf-8 -*-
r"""sing_svc 引擎（agent 进程内）：so-vits-svc 整合包生命周期 + 模型库 + 转换链路。

- 整合包要求：so-vits-svc 4.1 系 Windows 整合包（含 runtime\python.exe 与
  inference\infer_tool.py——官方仓库结构；训练不需要，只要推理部分）
- 引擎进程：用整合包的 runtime\python.exe 跑本插件自带的 svc_server.py
  （cwd=整合包根，依赖全解析自整合包 runtime），端口默认 6843
- 模型库：社区 svc 模型包（G*.pth + config.json [+ kmeans*.pt]）导入到
  DATA_DIR/svc_models/<id>/；id 走字符白名单（a-z0-9_），无越界可能
- 转换：NapCat get_record→mp3 → ffmpeg 转 32k mono wav → /convert →
  整曲 wav → cache/tts/（send_voice 白名单目录）
"""
import hashlib
import http.client
import json
import os
import pathlib as _pl
import re
import shutil as _sh
import subprocess
import time

import qq_common as C

DATA_DIR = C.DATA_DIR
MODELS_ROOT = os.path.realpath(os.path.join(DATA_DIR, "svc_models"))
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(DATA_DIR, "logs", "svc_engine.log")

_proc = {"p": None}
_busy = {"until": 0.0}

_GSV_FFMPEG_CANDIDATES = [
    r"E:\GPTSoVITS\GPT-SoVITS-v3lora-20250228\ffmpeg.exe",
]


def log(msg):
    try:
        with open(LOG_PATH, "ab") as f:
            f.write(("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)).encode("utf-8"))
    except OSError:
        pass


def default_cfg():
    return {"dir": "", "port": 6843, "autostart": False, "default_model": "",
            "tran": 0, "max_sec": 120, "spk": "", "runtime": ""}


# ---------------- Runtime / 整合包探测 ----------------
# 三种引擎形态，效力等同：
#   ① 完整整合包（dir 下自带 runtime\python.exe）
#   ② 源码 + 复用 GPT-SoVITS 整合包的 runtime（一键部署自动拼装：GSV runtime
#      自带 fairseq/librosa/sklearn 等全部推理依赖，实测可跑 4.1 推理）
#   ③ 源码 + cfg.runtime 指定的外部 runtime

def _gsv_runtime_dirs():
    """本机 GPT-SoVITS 整合包 runtime 探测（含引擎卡配置的目录优先）。"""
    found = []
    try:
        import qq_tts as _T
        gdir = _T.gsv_dir({"gsv_dir": ""}) or ""
        if gdir:
            p = os.path.join(gdir, "runtime", "python.exe")
            if os.path.isfile(p):
                found.append(os.path.join(gdir, "runtime"))
    except Exception:
        pass
    for root in ("E:\\GPTSoVITS", "E:\\", "D:\\", "D:\\AI", "E:\\AI"):
        if not os.path.isdir(root):
            continue
        try:
            for name in sorted(os.listdir(root)):
                rp = os.path.join(root, name, "runtime")
                if os.path.isfile(os.path.join(rp, "python.exe")) and rp not in found:
                    # 认 GPT-SoVITS 特征（GPT_SoVITS 目录）防误报其他 python
                    if os.path.isdir(os.path.join(root, name, "GPT_SoVITS")):
                        found.append(rp)
        except OSError:
            continue
    return found


def runtime_python(cfg):
    """引擎用哪个 python：本目录 runtime > cfg.runtime > GSV 探测；无则空。"""
    d = str(cfg.get("dir") or "").strip()
    if d:
        p = os.path.join(d, "runtime", "python.exe")
        if os.path.isfile(p):
            return p
    ext = str(cfg.get("runtime") or "").strip()
    if ext:
        p = os.path.join(ext, "python.exe")
        if os.path.isfile(p):
            return p
    for rp in _gsv_runtime_dirs():
        return os.path.join(rp, "python.exe")
    return ""


def svc_dir_ok(cfg):
    r"""引擎目录就绪判定：源码 inference\infer_tool.py 必需 + 可用 runtime。"""
    d = str((cfg or {}).get("dir") or "").strip()
    if not d or not os.path.isfile(os.path.join(d, "inference", "infer_tool.py")):
        return False
    return bool(runtime_python(cfg))


def detect_svc_dirs():
    """扫本机现成的完整整合包（含自带 runtime 的）。"""
    found = []
    for root in ("E:\\", "D:\\", "E:\\GPTSoVITS", "D:\\AI", "E:\\AI"):
        if not os.path.isdir(root):
            continue
        try:
            for name in os.listdir(root):
                p = os.path.join(root, name)
                if os.path.isdir(p) and os.path.isfile(os.path.join(p, "runtime", "python.exe")) \
                        and os.path.isfile(os.path.join(p, "inference", "infer_tool.py")) \
                        and p not in found:
                    found.append(p)
        except OSError:
            continue
    return found


# ---------------- 一键部署（源码 zip + 复用 runtime + ContentVec 底模） ----------------

SRC_ZIP_URLS = (
    "https://github.com/svc-develop-team/so-vits-svc/archive/refs/heads/4.1-Stable.zip",
    "https://gh-proxy.com/https://github.com/svc-develop-team/so-vits-svc/archive/refs/heads/4.1-Stable.zip",
)
CVEC_URLS = (
    "https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt",
    "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt",
)

_setup = {"phase": "", "pct": 0, "msg": "", "err": "", "t": 0.0}


def setup_status():
    return dict(_setup)


def _setup_tick(phase, pct, msg):
    _setup.update(phase=phase, pct=int(pct), msg=msg, err="", t=time.time())
    log("setup %s %d%% %s" % (phase, pct, msg))


def _download(url, dst, label, attempts=6):
    """流式下载（http.client；1MB 块；进度写 _setup）。
    断点续传：hf-mirror 这类镜像会中途失速（实测 360MB 拉到一半 read 超时），
    .part 落盘保留，重试带 Range 从断点继续，成功才 rename 到目标。
    写法合规：先校验 https 与公网 host；断点/目标路径走 pathlib 派生。"""
    import ssl
    import urllib.parse
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise RuntimeError("仅允许 https 下载源")
    host = u.hostname.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") \
            or host.startswith("192.168.") or host.startswith("10.") \
            or host.startswith("172.") or host == "metadata.google.internal":
        raise RuntimeError("下载源必须是公网地址")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    p_dst = _pl.Path(dst)
    part = p_dst.with_name(p_dst.name + ".part")   # 断点文件（同目录同名 +.part）
    last_err = ""
    for _att in range(attempts):
        try:
            got = part.stat().st_size if part.is_file() else 0
            headers = {"User-Agent": "zcode-qq-bot"}
            if got:
                headers["Range"] = "bytes=%d-" % got
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, timeout=90, context=ctx)
            path = u.path + (("?" + u.query) if u.query else "")
            conn.request("GET", path, headers=headers)
            r = conn.getresponse()
            if r.status not in (200, 206):
                conn.close()
                raise RuntimeError("HTTP %d" % r.status)
            if r.status == 200:
                got = 0          # 服务器不支持断点：重头
            clen = r.getheader("Content-Length") or "0"
            total = int(clen) + (got if r.status == 206 else 0)
            with part.open("ab" if got else "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        _setup.update(
                            pct=min(96, 4 + int(got * 92 / max(total, 1))),
                            msg="%s %d/%d MB%s" % (label, got // 1048576,
                                                   total // 1048576,
                                                   "（续传）" if _att else ""))
            conn.close()
            if total and got < total:
                raise RuntimeError("提前断流 %d/%d" % (got, total))
            os.replace(str(part), dst)
            return got
        except Exception as e:
            last_err = str(e)
            log("download attempt%d fail %s %r（保留断点）" % (_att + 1, url[:60], e))
            time.sleep(3)
    raise RuntimeError("%s 下载失败（已重试 %d 次）：%s" % (label, attempts, last_err[:120]))


def _download_first(urls, dst, label):
    """按序试多个源（主→镜像）；每个源内部自带断点续传重试。"""
    last = ""
    for url in urls:
        try:
            _download(url, dst, label)
            return True
        except Exception as e:
            last = str(e)
            log("download fail %s %r" % (url[:60], e))
    raise RuntimeError("%s 下载失败（主/镜像都试过）：%s" % (label, last[:120]))


def setup_run(install_root, cfg):
    """一键部署：①源码 zip→解压 ②runtime 解析（复用 GSV）③ContentVec 底模。
    install_root = 安装根目录（如 E:\\SoVITS）；源码落在其下 so-vits-svc-4.1/。"""
    import zipfile
    root = os.path.normpath(os.path.abspath(os.path.expanduser(str(install_root or "").strip())))
    if not root or not os.path.isdir(os.path.dirname(root) or "."):
        _setup.update(err="安装目录无效")
        return {"ok": False, "error": "安装目录无效"}
    svc = os.path.join(root, "so-vits-svc-4.1")
    try:
        # ① 源码
        if not os.path.isfile(os.path.join(svc, "inference", "infer_tool.py")):
            _setup_tick("src", 2, "下载官方源码（4.1-Stable，约几 MB）…")
            zpath = os.path.join(root, "_svc_src.zip")
            _download_first(SRC_ZIP_URLS, zpath, "源码包")
            _setup_tick("src", 30, "解压源码…")
            with zipfile.ZipFile(zpath) as z:
                names = z.namelist()
                top = names[0].split("/")[0]
                z.extractall(root)
            os.replace(os.path.join(root, top), svc)
            try:
                os.remove(zpath)
            except OSError:
                pass
        _setup_tick("runtime", 40, "解析 Python 运行时…")
        # ② runtime：svc 自带 > cfg.runtime > GSV
        py = runtime_python({**cfg, "dir": svc})
        how = ""
        if os.path.isfile(os.path.join(svc, "runtime", "python.exe")):
            how = "自带 runtime"
        else:
            gsvs = _gsv_runtime_dirs()
            if not py:
                _setup.update(err="no-runtime",
                              msg="没找到可复用的 Python 运行时。两个办法：①装 GPT-SoVITS "
                                  "整合包（语音消息页有引导，装完回来点「继续部署」自动复用）；"
                                  "②手动放一个 so-vits-svc 整合包目录")
                return {"ok": False, "error": "no-runtime", "need": "gsv"}
            how = "复用 GPT-SoVITS runtime（%s）" % os.path.dirname(os.path.dirname(py))
        # ③ ContentVec 底模
        cvec = os.path.join(svc, "pretrain", "checkpoint_best_legacy_500.pt")
        if not os.path.isfile(cvec):
            _setup_tick("cvec", 42, "下载 ContentVec 底模（约 360MB，hf-mirror 优先）…")
            _download_first(CVEC_URLS, cvec, "底模")
        _setup_tick("done", 100, "部署完成（%s）" % how)
        return {"ok": True, "dir": svc, "runtime": how}
    except Exception as e:
        _setup.update(err=str(e)[:200])
        log("setup FAIL %r" % e)
        return {"ok": False, "error": str(e)[:200]}


# ---------------- ffmpeg（输入规范化 mp3→wav） ----------------

def find_ffmpeg(cfg):
    cands = []
    d = str(cfg.get("dir") or "")
    if d:
        cands += [os.path.join(d, "ffmpeg.exe"),
                  os.path.join(d, "runtime", "ffmpeg.exe")]
    for rp in _gsv_runtime_dirs():
        cands.append(os.path.join(os.path.dirname(rp), "ffmpeg.exe"))
    cands += _GSV_FFMPEG_CANDIDATES
    w = _sh.which("ffmpeg")
    if w:
        cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def to_wav(src, dst, cfg):
    """音频 → 32k 单声道 wav（svc 输入规范）。src 只作只读输入。"""
    ff = find_ffmpeg(cfg)
    if not ff:
        return {"ok": False,
                "error": "没找到 ffmpeg（整合包一般自带；或装 GPT-SoVITS 整合包后自动复用）"}
    try:
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
             "-ac", "1", "-ar", "32000", dst],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not os.path.isfile(dst):
            return {"ok": False,
                    "error": "ffmpeg 转码失败：%s" % r.stderr.decode("utf-8", "replace")[:160]}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": "ffmpeg 执行失败：%s" % e}


def wav_duration(path, cfg):
    ff = find_ffmpeg(cfg)
    if not ff:
        return 0.0
    try:
        pr = subprocess.run([ff, "-i", path, "-f", "null", "-"],
                            capture_output=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tail = pr.stderr.decode("utf-8", "replace").rsplit("time=", 1)[-1]
        parts = tail.split(" ")[0].split(":")
        return sum(float(x) * (60 ** i) for i, x in enumerate(reversed(parts)))
    except Exception:
        return 0.0


# ---------------- 引擎进程 ----------------

def engine_status(cfg):
    port = int(cfg.get("port") or 6843)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/status")
        r = c.getresponse()
        d = json.loads(r.read().decode("utf-8"))
        c.close()
        return {"ok": True, "running": True,
                "ours": bool(_proc["p"] and _proc["p"].poll() is None), **d}
    except Exception:
        return {"ok": True, "running": False, "ours": False}


def engine_start(cfg):
    d = str(cfg.get("dir") or "")
    if not svc_dir_ok(cfg):
        py = runtime_python(cfg)
        return {"ok": False,
                "error": "引擎目录未就绪（缺 inference\\infer_tool.py%s）"
                % ("" if py else " 或可用的 Python 运行时——装 GPT-SoVITS 整合包后可自动复用")}
    st = engine_status(cfg)
    if st.get("running"):
        return {"ok": True, "already": True, **st}
    py = runtime_python(cfg)
    port = int(cfg.get("port") or 6843)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logf = open(LOG_PATH, "ab")
    try:
        p = subprocess.Popen(
            [py, os.path.join(PLUGIN_DIR, "svc_server.py"), str(port)],
            cwd=d, stdout=logf, stderr=logf,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        return {"ok": False, "error": "启动失败：%s" % e}
    _proc["p"] = p
    # 端口就绪（torch 首次 import 可到 1-2 分钟）
    for _ in range(60):
        time.sleep(2)
        if p.poll() is not None:
            return {"ok": False, "error": "引擎进程退出（详见 logs/svc_engine.log）"}
        if engine_status(cfg).get("running"):
            return {"ok": True, "started": True, **engine_status(cfg)}
    return {"ok": False, "error": "启动超时（torch 加载慢，稍后刷新状态看看）"}


def engine_stop():
    p = _proc.get("p")
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
        _proc["p"] = None
        return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False, "note": "没有本插件启动的引擎"}


# ---------------- 模型库 ----------------

_MODEL_ID_RE = re.compile(r"^[a-z0-9_]{2,12}$")
_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def _idx_path():
    return os.path.join(MODELS_ROOT, "index.json")


def _idx_read():
    try:
        d = json.loads(_pl.Path(_idx_path()).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _idx_write(idx):
    os.makedirs(MODELS_ROOT, exist_ok=True)
    _pl.Path(_idx_path()).write_text(
        json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


def _model_dir(mid):
    """模型 id → 受控根下的一级子目录。id 走严格字符白名单（仅 a-z0-9_，
    无点无斜杠），join 后必然落在受控根一层之内，无穿越可能。"""
    mid = str(mid or "").strip()
    if not _MODEL_ID_RE.fullmatch(mid) or not set(mid) <= _SAFE_CHARS:
        return None
    return os.path.normpath(os.path.join(MODELS_ROOT, mid))


def _scan_model_files(src):
    """列模型包三元组（pth/cluster/有无 config）。只读。"""
    try:
        names = os.listdir(src)
    except OSError:
        return "", "", False
    pths = [n for n in sorted(names)
            if n.lower().endswith(".pth") and n.lower().startswith("g")]
    if not pths:
        pths = [n for n in sorted(names) if n.lower().endswith(".pth")]
    clu = ""
    for n in sorted(names):
        if "kmeans" in n.lower() and n.lower().endswith(".pt"):
            clu = n
            break
    has_cfg = any(n.lower() == "config.json" for n in names)
    return (pths[0] if pths else ""), clu, has_cfg


def models_list():
    out = []
    for mid, m in _idx_read().items():
        d = _model_dir(mid)
        if not d or not os.path.isdir(d) or not isinstance(m, dict):
            continue
        pth = [f for f in os.listdir(d) if f.lower().endswith(".pth")]
        out.append({"id": mid, "name": str(m.get("name") or mid)[:40],
                    "created": m.get("created", 0),
                    "ok": bool(pth) and os.path.isfile(os.path.join(d, "config.json")),
                    "spk": m.get("spk", "")})
    out.sort(key=lambda x: -int(x.get("created") or 0))
    return out


def model_meta(mid):
    d = _model_dir(mid)
    if not d or not os.path.isdir(d):
        return None, {}
    return d, _idx_read().get(os.path.basename(d)) or {}


def model_import(src_dir, name):
    """导入社区模型包：src_dir 含 G*.pth + config.json（kmeans*.pt 可选）。
    源目录只读；复制目标恒为受控模型库（id 由白名单生成）。"""
    nm = str(name or "").strip()[:32]
    if not nm:
        return {"ok": False, "error": "给模型起个音色名"}
    raw = str(src_dir or "").strip()
    src = os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
    if not src or not os.path.isdir(src):
        return {"ok": False, "error": "源文件夹不存在"}
    pth_name, clu_name, has_cfg = _scan_model_files(src)
    if not pth_name:
        return {"ok": False, "error": "文件夹里没找到 .pth 模型文件"}
    if not has_cfg:
        return {"ok": False, "error": "文件夹里没找到 config.json（应与 .pth 同目录）"}
    src_pth = os.path.join(src, pth_name)
    if os.path.getsize(src_pth) < 1024 * 1024:
        return {"ok": False, "error": ".pth 小于 1MB，不像歌声模型"}
    mid = "m" + hashlib.sha256(
        (nm + str(time.time())).encode("utf-8")).hexdigest()[:8]
    dst = _model_dir(mid)
    if dst is None:
        return {"ok": False, "error": "id 生成异常"}
    os.makedirs(dst, exist_ok=True)
    try:
        _sh.copy2(src_pth, os.path.join(dst, "model.pth"))
        _sh.copy2(os.path.join(src, "config.json"), os.path.join(dst, "config.json"))
        cluster = ""
        if clu_name:
            _sh.copy2(os.path.join(src, clu_name), os.path.join(dst, "cluster.pt"))
            cluster = "cluster.pt"
    except OSError as e:
        return {"ok": False, "error": "复制模型失败：%r" % e}
    # 说话人名：config 的 speakers 第一个键（多数社区包单说话人）
    spk = ""
    try:
        jj = json.loads(_pl.Path(os.path.join(dst, "config.json")).read_text(
            encoding="utf-8"))
        spk = list((jj.get("speakers") or {}).keys())[0] if jj.get("speakers") else ""
    except Exception:
        pass
    idx = _idx_read()
    idx[mid] = {"name": nm, "created": int(time.time()),
                "cluster": cluster, "spk": spk}
    _idx_write(idx)
    log("model imported %s=%s spk=%s" % (mid, nm, spk))
    return {"ok": True, "id": mid, "name": nm, "spk": spk}


def model_del(mid):
    d = _model_dir(mid)
    if not d or not os.path.isdir(d):
        return {"ok": False, "error": "模型不存在"}
    dead = os.path.join(d, ".del")
    try:
        os.rename(d, dead)
        _sh.rmtree(dead, ignore_errors=True)
    except OSError:
        pass
    idx = _idx_read()
    idx.pop(str(mid), None)
    _idx_write(idx)
    return {"ok": True}


# ---------------- 转换主链路 ----------------

def _engine_call(cfg, method, path, payload=None, timeout=240):
    port = int(cfg.get("port") or 6843)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        c.request(method, path, body=body,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return json.loads(r.read().decode("utf-8"))
    finally:
        c.close()


_loaded = {"key": ""}


def ensure_model(mid, cfg):
    """确保引擎在跑且已加载目标模型。返回 {ok, error?}。"""
    st = engine_status(cfg)
    if not st.get("running"):
        r = engine_start(cfg)
        if not r.get("ok"):
            return r
        st = engine_status(cfg)
    d, m = model_meta(mid)
    if not d:
        return {"ok": False, "error": "模型不存在（可能已被删除）"}
    key = "|".join([str(mid), str(m.get("spk", "")), str(m.get("cluster", ""))])
    if _loaded["key"] == key and st.get("model"):
        return {"ok": True}
    cluster = str(m.get("cluster") or "")
    clu = os.path.join(d, cluster) if cluster else ""
    if clu and not os.path.isfile(clu):
        clu = ""
    r = _engine_call(cfg, "POST", "/load",
                     {"model_path": os.path.join(d, "model.pth"),
                      "config_path": os.path.join(d, "config.json"),
                      "cluster_path": clu}, timeout=180)
    if r.get("ok"):
        _loaded["key"] = key
        log("model loaded %s (%ss)" % (mid, r.get("load_sec")))
    return r


def convert(mp3_path, mid, cfg):
    """mp3 → 指定模型音色的整曲 wav（写到 cache/tts/ 供发送）。"""
    if _busy["until"] and time.time() < _busy["until"]:
        return {"ok": False, "error": "上一首还在转换，稍等一下再点"}
    _busy["until"] = time.time() + 300
    try:
        r = ensure_model(mid, cfg)
        if not r.get("ok"):
            return r
        tmp_dir = os.path.join(DATA_DIR, "cache", "records")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_wav = os.path.join(tmp_dir, "svc_in_%d.wav" % int(time.time()))
        r2 = to_wav(mp3_path, tmp_wav, cfg)
        if not r2.get("ok"):
            return r2
        dur = wav_duration(tmp_wav, cfg)
        max_sec = int(cfg.get("max_sec") or 120)
        if dur and dur > max_sec:
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
            return {"ok": False,
                    "error": "这段语音约 %.0f 秒，超过上限 %d 秒——剪短一点（清唱干声效果最好）"
                    % (dur, max_sec)}
        d, m = model_meta(mid)
        tts_dir = os.path.join(DATA_DIR, "cache", "tts")
        os.makedirs(tts_dir, exist_ok=True)
        out = os.path.join(tts_dir, "sing_%d.wav" % int(time.time() * 1000))
        spk = str(cfg.get("spk") or "") or str(m.get("spk") or "") or 0
        tran = int(cfg.get("tran") or 0)
        r3 = _engine_call(cfg, "POST", "/convert",
                          {"wav_path": tmp_wav, "spk": spk, "tran": tran,
                           "out_path": out})
        try:
            os.remove(tmp_wav)
        except OSError:
            pass
        if not r3.get("ok"):
            log("convert FAIL %s" % r3.get("error"))
            return r3
        log("convert ok %s dur=%ss engine=%ss" % (mid, r3.get("dur"), r3.get("sec")))
        return {"ok": True, "path": out, "dur": r3.get("dur"),
                "sec": r3.get("sec"), "name": m.get("name", "")}
    finally:
        _busy["until"] = 0.0
