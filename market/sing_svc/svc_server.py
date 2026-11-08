# -*- coding: utf-8 -*-
r"""svc_server.py —— 由插件拉起、运行在 so-vits-svc 整合包环境里的推理服务。

为什么不用官方 flask_api：官方两份 api 都把模型路径写死在进程启动时，
切音色=改脚本重启；且用 multipart/服务器本地路径等杂糅约定。这里给一个
纯标准库 http.server 的小服务（不依赖 flask），支持：
  GET  /status                 → {ok, model, speakers, sr}
  POST /load  {model_path, config_path, cluster_path}
  POST /convert {wav_path, spk, tran, out_path}
     wav_path/out_path 是本机绝对路径（插件与引擎同机）；spk 支持说话人
     名或 id；tran 半音数。转换完把整曲 wav 写到 out_path。
推理逻辑对齐官方 flask_api_full_song.py（切片→逐段 infer→拼接）。
启动：整合包 runtime\python.exe svc_server.py <port>（cwd=整合包根目录）
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()          # torch 推理不并发：一次一段
_state = {"svc": None, "model": "", "speakers": {}, "sr": 32000, "err": ""}


def _do_load(p):
    model_path = str(p.get("model_path") or "")
    config_path = str(p.get("config_path") or "")
    cluster_path = str(p.get("cluster_path") or "") or None
    if not (os.path.isfile(model_path) and os.path.isfile(config_path)):
        return {"ok": False, "error": "模型或 config 文件不存在"}
    try:
        t0 = time.time()
        from inference import infer_tool
        svc = infer_tool.Svc(model_path, config_path,
                             cluster_model_path=cluster_path)
        speakers = {}
        try:
            cfg = json.loads(open(config_path, encoding="utf-8").read())
            speakers = cfg.get("speakers") or {}
        except Exception:
            pass
        _state.update(svc=svc, model=os.path.basename(model_path),
                      speakers=speakers, sr=getattr(svc, "target_sample", 32000),
                      err="")
        return {"ok": True, "model": _state["model"],
                "speakers": list(speakers.keys()),
                "load_sec": round(time.time() - t0, 1)}
    except Exception as e:
        _state["err"] = repr(e)
        return {"ok": False, "error": "加载失败：%r" % e}


def _do_convert(p):
    if _state["svc"] is None:
        return {"ok": False, "error": "还没有加载模型（先 /load）"}
    wav_path = str(p.get("wav_path") or "")
    out_path = str(p.get("out_path") or "")
    spk = p.get("spk", 0)
    tran = int(p.get("tran") or 0)
    if not (os.path.isfile(wav_path) and out_path):
        return {"ok": False, "error": "wav 不存在或缺 out_path"}
    try:
        import numpy as np
        import soundfile
        from inference import infer_tool, slicer
        svc = _state["svc"]
        # spk：名字 → id（config 的 speakers 映射）；直接数字就原样传
        if isinstance(spk, str) and not spk.isdigit():
            spk = _state["speakers"].get(spk, spk)
        elif isinstance(spk, str) and spk.isdigit():
            spk = int(spk)
        svc_model = svc
        chunks = slicer.cut(wav_path, db_thresh=-40)
        audio_data, audio_sr = slicer.chunks2audio(wav_path, chunks)
        audio = []
        t0 = time.time()
        for (slice_tag, data) in audio_data:
            length = int(np.ceil(len(data) / audio_sr * svc_model.target_sample))
            if slice_tag:
                _audio = np.zeros(length)
            else:
                pad_len = int(audio_sr * 0.5)
                data = np.concatenate([np.zeros([pad_len]), data, np.zeros([pad_len])])
                raw_path = __import__("io").BytesIO()
                soundfile.write(raw_path, data, audio_sr, format="wav")
                raw_path.seek(0)
                out_audio, _sr = svc_model.infer(spk, tran, raw_path)
                svc_model.clear_empty()
                _audio = out_audio.cpu().numpy()
                pad_len = int(svc_model.target_sample * 0.5)
                _audio = _audio[pad_len:-pad_len]
            audio.extend(list(infer_tool.pad_array(_audio, length)))
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        soundfile.write(out_path, np.array(audio), svc_model.target_sample,
                        format="wav")
        return {"ok": True, "path": out_path,
                "dur": round(len(audio) / float(svc_model.target_sample), 1),
                "sec": round(time.time() - t0, 1)}
    except Exception as e:
        return {"ok": False, "error": "转换失败：%r" % e}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[svc] %s\n" % (fmt % args))
        sys.stderr.flush()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            return self._json(200, {"ok": True, "model": _state["model"],
                                    "speakers": list(_state["speakers"].keys()),
                                    "sr": _state["sr"], "err": _state["err"]})
        self._json(404, {"ok": False})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            p = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})
        with _lock:
            if self.path == "/load":
                return self._json(200, _do_load(p))
            if self.path == "/convert":
                return self._json(200, _do_convert(p))
        self._json(404, {"ok": False})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 6843
    # 关键：整合包代码（inference/ 等）在 cwd——脚本自身目录在 sys.path[0]，
    # 不把 cwd 插进去 import 不到包（mock 全链测试抓到的第一个坑）
    sys.path.insert(0, os.getcwd())
    print("[svc] listening on 127.0.0.1:%d" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
