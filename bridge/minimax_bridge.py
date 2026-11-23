#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax H3 v2 协议 ↔ pika_h3 Ref2VA 工作流 桥接服务（纯标准库）

协议对齐（2026-11 官方文档）：
  POST   /v2/video_generation                创建任务 → {"task_id": "..."}
  GET    /v2/query/video_generation/{id}     查询 → {"task": {...}}（response_format=flat 时摊平）
  GET    /v2/query/video_generation          任务列表
  DELETE /v2/video_generation/{id}           取消 / 删除
  GET    /v2/files/{id}.mp4                  产物下载（task.content.url 指向这里）
  GET    /health

创建请求（v2）：
  model        MiniMax-H3 | MiniMax-H3-Highspeed | MiniMax-H3-Lite | MiniMax-H3-Max
  content      多模态数组：
                {"type":"text","text":"..."}
                {"type":"image_url","image_url":{"url":"..."},"role":"reference_image"}
                {"type":"video_url","video_url":{"url":"..."},"role":"reference_video"}
                {"type":"audio_url","audio_url":{"url":"..."},"role":"reference_audio"}
  resolution   768P | 2K
  duration     4-15（秒）
  ratio        adaptive | 16:9 | 4:3 | 1:1 | 3:4 | 9:16 | 21:9
  generate_audio  bool（false 时产物去音轨）
  callback_url    任务状态变更时 POST 同一个 task 体
  response_format task（默认）| flat

本桥接扩展（协议里没有，按需用）：
  two_stage     true/false。默认由 resolution 决定：768P→一阶段，2K→二阶段（×2 细化）
  ref_image_size match | max
  seed          整数

媒体 URL 支持三种写法：http(s)、data:（base64）、本地路径。

用法：
  python3 bridge/minimax_bridge.py --port 8099 --public-base http://192.168.1.8:8099
"""

import argparse
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import h3_api  # noqa: E402

MAX_IMAGES, MAX_AUDIOS, MAX_VIDEOS = 9, 3, 3

# 768P / 2K 的规范尺寸（均为 32 的倍数，2K = 768P × 2）
SIZE_TABLE = {
    "768P": {"16:9": (1344, 768), "4:3": (1024, 768), "1:1": (768, 768),
             "3:4": (768, 1024), "9:16": (768, 1344), "21:9": (1792, 768)},
    "2K": {"16:9": (2688, 1536), "4:3": (2048, 1536), "1:1": (1536, 1536),
           "3:4": (1536, 2048), "9:16": (1536, 2688), "21:9": (3584, 1536)},
}
MODELS = {"MiniMax-H3", "MiniMax-H3-Highspeed", "MiniMax-H3-Lite", "MiniMax-H3-Max"}
MEDIA_KEYS = {"image_url": "images", "video_url": "videos", "audio_url": "audios"}


class ApiError(Exception):
    def __init__(self, message, code=400, type_="bad_request_error"):
        super().__init__(message)
        self.message, self.code, self.type = message, code, type_


def norm_ratio(ratio):
    r = (ratio or "16:9").strip()
    if r == "adaptive":
        return "16:9"
    if r not in SIZE_TABLE["768P"]:
        raise ApiError(f"ratio 不支持：{r}（可选 {sorted(SIZE_TABLE['768P']) + ['adaptive']}）")
    return r


def fetch_media(url):
    """支持 http(s) / data: / 本地路径，返回 (bytes, 后缀)。"""
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        m = re.search(r"image/(\w+)|video/(\w+)|audio/(\w+)", head)
        ext = "." + next((g for g in (m.groups() if m else []) if g), "bin")
        return base64.b64decode(b64), ext
    if url.startswith(("http://", "https://")):
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
        ext = pathlib.Path(urllib.parse.urlparse(url).path).suffix or ".bin"
        return data, ext
    p = pathlib.Path(url)
    if not p.exists():
        raise ApiError(f"媒体文件不存在：{url}")
    return p.read_bytes(), p.suffix


# ---------------------------------------------------------------- 任务存储
class JobStore:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)
        (self.root / "outputs").mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.lock = threading.RLock()
        self.jobs = {}
        if self.index_path.exists():
            try:
                self.jobs = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                self.jobs = {}

    def _flush(self):
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.jobs, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    def add(self, job):
        with self.lock:
            self.jobs[job["id"]] = job
            self._flush()
        return job

    def update(self, task_id, **fields):
        with self.lock:
            job = self.jobs.get(task_id)
            if not job:
                return None
            job.update(fields)
            job["updated_at"] = int(time.time())
            self._flush()
            return job

    def get(self, task_id):
        with self.lock:
            return self.jobs.get(task_id)

    def all(self):
        with self.lock:
            return list(self.jobs.values())

    def job_dir(self, task_id):
        d = self.root / "jobs" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def output_path(self, task_id):
        return self.root / "outputs" / f"{task_id}.mp4"


# ---------------------------------------------------------------- 主体
class Bridge:
    def __init__(self, args):
        self.args = args
        self.comfy = h3_api.Comfy(args.base)
        self.store = JobStore(args.workdir)
        self.queue, self.qcv = [], threading.Condition()
        here = pathlib.Path(__file__).resolve().parent.parent

        def resolve(p):
            q = pathlib.Path(p)
            return q if q.exists() else here / p

        self.templates = {
            "one": json.loads(resolve(args.template_one_stage).read_text(encoding="utf-8")),
            "two": json.loads(resolve(args.template_two_stage).read_text(encoding="utf-8")),
        }

    # -- 对外 task 体 ------------------------------------------------------
    def task_body(self, job):
        status_map = {"queued": "queued", "in_progress": "running",
                      "completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}
        task = {
            "id": job["id"],
            "model": job["model"],
            "status": status_map.get(job["status"], job["status"]),
            "created_at": job["created_at"],
            "updated_at": job.get("updated_at", job["created_at"]),
            "resolution": job["resolution"],
            "size": job.get("size"),          # 扩展字段：实际输出尺寸，便于核对
            "duration": job["duration"],
            "ratio": job["ratio"],
            "task_type": "generation",
            "modality": "video",
            "usage": {
                "total_seconds": job["duration"],
                "input_seconds": 0,
                "output_seconds": job["duration"] if job["status"] == "completed" else 0,
                "input_image_count": len(job["files"]["images"]),
            },
        }
        if job["status"] == "completed":
            task["content"] = {"url": f"{self.args.public_base.rstrip('/')}/v2/files/{job['id']}.mp4"}
        if job.get("error"):
            task["error"] = job["error"]
        if job.get("progress") is not None and job["status"] == "in_progress":
            task["progress"] = job["progress"]
        return task

    # -- 创建 -------------------------------------------------------------
    def create(self, payload):
        if not isinstance(payload, dict):
            raise ApiError("请求体必须是 JSON 对象")
        model = payload.get("model") or "MiniMax-H3"
        resolution = str(payload.get("resolution") or "768P").upper()
        if resolution not in SIZE_TABLE:
            raise ApiError(f"resolution 不支持：{resolution}（可选 768P / 2K）")
        ratio = norm_ratio(payload.get("ratio"))
        try:
            duration = int(payload.get("duration"))
        except (TypeError, ValueError):
            raise ApiError("duration 必填，整数秒（4-15）")
        if not (1 <= duration <= 20):
            raise ApiError(f"duration 超出范围：{duration}（本桥接接受 1-20 秒）")

        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise ApiError("content 必须是非空数组")

        texts, images, videos, audios = [], [], [], []
        for item in content:
            if not isinstance(item, dict):
                raise ApiError("content 的每一项必须是对象")
            t = item.get("type")
            if t == "text":
                if item.get("text"):
                    texts.append(item["text"])
                continue
            if t not in MEDIA_KEYS:
                raise ApiError(f"content.type 不支持：{t}")
            role = item.get("role")
            if t == "image_url" and role in ("first_frame", "last_frame"):
                raise ApiError(
                    "本桥接基于 Ref2VA 工作流，只支持 role=reference_image / reference_video / "
                    "reference_audio；首尾帧需要 FL2VA 模型，容器里尚未安装")
            bucket = {"image_url": images, "video_url": videos, "audio_url": audios}[t]
            url = (item.get(t) or {}).get("url")
            if not url:
                raise ApiError(f"{t} 缺少 {t}.url")
            bucket.append(url)

        if not texts:
            raise ApiError("content 必须包含至少一条非空 text")
        if len(images) > MAX_IMAGES:
            raise ApiError(f"参考图最多 {MAX_IMAGES} 张，收到 {len(images)}")
        if len(audios) > MAX_AUDIOS:
            raise ApiError(f"参考音频最多 {MAX_AUDIOS} 条，收到 {len(audios)}")
        if len(videos) > MAX_VIDEOS:
            raise ApiError(f"参考视频最多 {MAX_VIDEOS} 段，收到 {len(videos)}")
        if not images and not videos:
            # MiniMax 规则：参考音频不能单独存在
            if audios:
                raise ApiError("参考音频不能单独使用，至少需要一张参考图或一段参考视频")

        # 二阶段：2K 默认开，768P 默认关；可用 two_stage 覆盖
        two_stage = payload.get("two_stage")
        two_stage = (resolution == "2K") if two_stage is None else bool(two_stage)
        base_w, base_h = SIZE_TABLE["768P"][ratio]
        if two_stage:
            gen_w, gen_h = base_w, base_h
            final_w, final_h = SIZE_TABLE["2K"][ratio]
        else:
            gen_w, gen_h = SIZE_TABLE[resolution][ratio]
            final_w, final_h = gen_w, gen_h

        # 扩展：自定义宽高（v2 协议没有，但很多人会传 size / width+height）。
        # 给的是“第一阶段生成尺寸”；two_stage=true 时最终仍 ×2。
        custom = None
        if payload.get("width") and payload.get("height"):
            custom = (int(payload["width"]), int(payload["height"]))
        elif isinstance(payload.get("size"), str) and re.match(r"^\s*\d+\s*[xX×]\s*\d+\s*$", payload["size"]):
            w_str, h_str = re.split(r"[xX×]", payload["size"])
            custom = (int(w_str), int(h_str))
        if custom:
            gen_w, gen_h = custom
            gen_w, gen_h = max(32, int(round(gen_w / 32) * 32)), max(32, int(round(gen_h / 32) * 32))
            final_w, final_h = (gen_w * 2, gen_h * 2) if two_stage else (gen_w, gen_h)

        task_id = "h3_" + uuid.uuid4().hex[:24]
        job_dir = self.store.job_dir(task_id)
        saved = {"images": [], "audios": [], "videos": []}
        for kind, urls in (("images", images), ("audios", audios), ("videos", videos)):
            for url in urls:
                data, ext = fetch_media(url)
                dest = job_dir / f"{kind}_{len(saved[kind])}{ext or '.bin'}"
                dest.write_bytes(data)
                saved[kind].append(str(dest))

        job = {
            "id": task_id,
            "model": model,
            "status": "queued",
            "progress": 0,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "prompt": "\n".join(texts),
            "resolution": resolution,
            "duration": duration,
            "ratio": ratio,
            "size": f"{final_w}x{final_h}",
            "gen_size": f"{gen_w}x{gen_h}",
            "two_stage": two_stage,
            "ref_image_size": payload.get("ref_image_size") or "match",
            "seed": payload.get("seed"),
            "generate_audio": payload.get("generate_audio", True) is not False,
            "callback_url": payload.get("callback_url"),
            "response_format": payload.get("response_format") or "task",
            "files": saved,
            "expect_seconds": 240 if two_stage else 90,
        }
        self.store.add(job)
        with self.qcv:
            self.queue.append(task_id)
            self.qcv.notify()
        return job

    # -- 回调 -------------------------------------------------------------
    def fire_callback(self, job):
        url = job.get("callback_url")
        if not url:
            return
        body = json.dumps({"task": self.task_body(job)}, ensure_ascii=False).encode()

        def send():
            try:
                req = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception as e:  # noqa: BLE001
                print(f"[bridge] callback 失败 {url}: {e}", flush=True)
        threading.Thread(target=send, daemon=True).start()

    # -- 工作线程 ---------------------------------------------------------
    def worker(self):
        while True:
            with self.qcv:
                while not self.queue:
                    self.qcv.wait()
                task_id = self.queue.pop(0)
            job = self.store.get(task_id)
            if not job or job["status"] != "queued":
                continue
            try:
                self.run_job(job)
            except BaseException as e:  # noqa: BLE001  线程不能因为 BaseException 死掉
                if isinstance(e, KeyboardInterrupt):
                    raise
                if not (isinstance(e, RuntimeError) and "实例不可用" in str(e)):
                    traceback.print_exc()
                self.store.update(task_id, status="failed", progress=100,
                                  error={"type": "generation_error", "message": str(e)})
                self.fire_callback(self.store.get(task_id))

    def preflight(self):
        """实例没就绪时快速失败，避免任务卡在 running。"""
        try:
            with urllib.request.urlopen(self.args.base.rstrip("/") + "/system_stats", timeout=15) as r:
                body = r.read()
            json.loads(body)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"ComfyUI 实例不可用（{self.args.base}）：{str(e)[:160]}")

    def run_job(self, job):
        task_id = job["id"]
        self.store.update(task_id, status="in_progress", started_at=time.time(), progress=1)
        self.preflight()
        self.fire_callback(self.store.get(task_id))

        refs = [self.comfy.upload(p) for p in job["files"]["images"]]
        auds = [self.comfy.upload(p) for p in job["files"]["audios"]]
        vids = [self.comfy.upload(p) for p in job["files"]["videos"]]

        prompt = json.loads(json.dumps(self.templates["two" if job["two_stage"] else "one"]))
        _, injected = h3_api.inject_refs(prompt, refs, auds, vids, job["ref_image_size"])
        gw, gh = (int(x) for x in job["gen_size"].split("x"))
        h3_api.set_named(prompt, 220, "value", job["prompt"])
        h3_api.set_named(prompt, 167, "value", float(job["duration"]))
        h3_api.set_named(prompt, 201, "value", gw)
        h3_api.set_named(prompt, 202, "value", gh)
        if job.get("seed") not in (None, ""):
            h3_api.set_named(prompt, 400, "value", int(job["seed"]))

        # 模板里 ref_image_size 被清空（null）时，H3 会按 2048 短边跑，慢数倍；强制回 match
        for t in (k for k, v in prompt.items() if v["class_type"] == "MiniMaxH3ReferenceToVideo"):
            if not prompt[t]["inputs"].get("ref_image_size"):
                prompt[t]["inputs"]["ref_image_size"] = "match"

        issues = h3_api.validate(prompt, self.comfy, ignore_combo_ids=injected)
        if issues:
            raise RuntimeError("prompt 校验未通过：" + "; ".join(issues[:5]))

        resp = self.comfy.submit(prompt)
        pid = resp.get("prompt_id")
        if not pid:
            raise RuntimeError(f"提交失败：{json.dumps(resp, ensure_ascii=False)[:500]}")
        self.store.update(task_id, prompt_id=pid)

        t0 = time.time()
        while True:
            h = self.comfy.history(pid)
            if pid in h:
                entry = h[pid]
                status = entry.get("status") or {}
                if entry.get("outputs") and status.get("status_str") != "error":
                    break
                if status.get("status_str") in ("success", "error") or status.get("completed"):
                    if not entry.get("outputs"):
                        raise RuntimeError(f"执行失败：{json.dumps(status, ensure_ascii=False)[:500]}")
                    break
            if time.time() - t0 > self.args.timeout:
                raise TimeoutError(f"等待超时（{self.args.timeout}s），prompt_id={pid}")
            time.sleep(5)
            expect = job.get("expect_seconds") or 120
            self.store.update(task_id,
                              progress=int(min(0.95, (time.time() - t0) / expect) * 100))

        dest = self.store.output_path(task_id)
        got = False
        for node_out in (entry.get("outputs") or {}).values():
            for v in node_out.values():
                if not isinstance(v, list):
                    continue
                for item in v:
                    if got or not isinstance(item, dict):
                        continue
                    if item.get("type", "output") != "output":
                        continue
                    if not item.get("filename", "").lower().endswith((".mp4", ".webm", ".mov")):
                        continue
                    q = urllib.parse.urlencode({"filename": item["filename"],
                                                "subfolder": item.get("subfolder", ""),
                                                "type": "output"})
                    with urllib.request.urlopen(self.args.base.rstrip("/") + "/view?" + q,
                                                timeout=900) as r:
                        dest.write_bytes(r.read())
                    got = True
        if not got:
            raise RuntimeError("没有拿到视频产物")

        if not job.get("generate_audio", True):
            ff = shutil.which("ffmpeg")
            if ff:
                stripped = dest.with_suffix(".noaudio.mp4")
                subprocess.run([ff, "-v", "error", "-y", "-i", str(dest), "-an",
                                "-c:v", "copy", str(stripped)], check=False)
                if stripped.exists() and stripped.stat().st_size > 0:
                    stripped.replace(dest)
            else:
                print("[bridge] generate_audio=false 但本机没有 ffmpeg，音轨未去除", flush=True)

        self.store.update(task_id, status="completed", progress=100,
                          completed_at=int(time.time()), file=str(dest))
        self.fire_callback(self.store.get(task_id))
        print(f"[bridge] {task_id} succeeded in {int(time.time()-t0)}s -> {dest}", flush=True)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    bridge: Bridge = None
    server_version = "MiniMaxH3Bridge/2.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[http] " + (fmt % a) + "\n")

    # -- helpers --
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-request-id", uuid.uuid4().hex)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message, type_="bad_request_error"):
        self._json(code, {"type": "error",
                          "error": {"type": type_, "message": message, "http_code": str(code)},
                          "request_id": uuid.uuid4().hex})

    def _authorized(self):
        key = self.bridge.args.api_key
        if not key:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {key}"

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ApiError(f"请求体不是合法 JSON：{e}")

    # -- routes --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if not self._authorized():
            return self._error(401, "invalid api key", "authentication_error")

        if path == "/health":
            return self._json(200, {"status": "ok", "base": self.bridge.args.base})

        m = re.match(r"^/v2/files/([^/]+)\.mp4$", path)
        if m:
            job = self.bridge.store.get(m.group(1))
            if not job or job["status"] != "completed":
                return self._error(404, "file not found", "not_found_error")
            data = pathlib.Path(job["file"]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        m = re.match(r"^/v2/query/video_generation/([^/]+)$", path)
        if m:
            job = self.bridge.store.get(m.group(1))
            if not job:
                return self._error(404, "task not found", "not_found_error")
            fmt = (qs.get("response_format") or [job.get("response_format") or "task"])[0]
            body = self.bridge.task_body(job)
            return self._json(200, body if fmt == "flat" else {"task": body})

        if path == "/v2/query/video_generation":
            jobs = sorted(self.bridge.store.all(), key=lambda j: j["created_at"], reverse=True)
            limit = int((qs.get("limit") or ["50"])[0])
            return self._json(200, {"items": [self.bridge.task_body(j) for j in jobs[:limit]],
                                    "total": len(jobs)})
        return self._error(404, f"unknown path {path}", "not_found_error")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return self._error(401, "invalid api key", "authentication_error")
        if path != "/v2/video_generation":
            return self._error(404, f"unknown path {path}", "not_found_error")
        try:
            job = self.bridge.create(self._read_json())
            return self._json(200, {"task_id": job["id"],
                                    "base_resp": {"status_code": 0, "status_msg": "success"}})
        except ApiError as e:
            return self._error(e.code, e.message, e.type)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, str(e), "server_error")

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return self._error(401, "invalid api key", "authentication_error")
        m = re.match(r"^/v2/video_generation/([^/]+)$", path)
        if not m:
            return self._error(404, f"unknown path {path}", "not_found_error")
        job = self.bridge.store.get(m.group(1))
        if not job:
            return self._error(404, "task not found", "not_found_error")
        with self.bridge.qcv:
            if job["id"] in self.bridge.queue:
                self.bridge.queue.remove(job["id"])
        self.bridge.store.update(job["id"], status="cancelled",
                                 error={"type": "cancelled", "message": "cancelled by client"})
        self.bridge.fire_callback(self.bridge.store.get(job["id"]))
        return self._json(200, {"task": self.bridge.task_body(self.bridge.store.get(job["id"]))})


def main():
    ap = argparse.ArgumentParser(description="MiniMax H3 v2 ↔ pika_h3 桥接服务")
    ap.add_argument("--base", default=os.environ.get("COMFY_BASE", h3_api.DEFAULT_BASE))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--public-base", default=os.environ.get("PUBLIC_BASE", ""),
                    help="task.content.url 使用的外部地址，默认 http://127.0.0.1:<port>")
    ap.add_argument("--workdir", default=str(pathlib.Path.home() / ".minimax_bridge"))
    ap.add_argument("--template-one-stage", default="pika_h3_stage1.api.json")
    ap.add_argument("--template-two-stage", default="pika_h3_2stage.api.json")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--api-key", default=os.environ.get("MINIMAX_BRIDGE_KEY"))
    args = ap.parse_args()
    if not args.public_base:
        args.public_base = f"http://127.0.0.1:{args.port}"

    bridge = Bridge(args)
    # 重启恢复：卡在 running 的置失败，还在排队的重新入队
    for job in bridge.store.all():
        if job["status"] == "in_progress":
            bridge.store.update(job["id"], status="failed", progress=100,
                                error={"type": "interrupted", "message": "桥接服务重启，任务中断"})
        elif job["status"] == "queued":
            bridge.queue.append(job["id"])
    threading.Thread(target=bridge.worker, daemon=True).start()
    Handler.bridge = bridge
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[bridge] listening on http://{args.host}:{args.port} -> {args.base}", flush=True)
    print(f"[bridge] content.url 前缀 {args.public_base}  workdir={args.workdir}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] bye", flush=True)


if __name__ == "__main__":
    main()
