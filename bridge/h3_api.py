#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 pika_h3 工作流编译成 API 模板，并用「参数化参考素材」调用它。

为什么不用画布导出的 JSON：
  - 导出的文件把参考图写死成固定节点、固定张数；
  - 未使用的槽位要么静音（导出即丢失），要么空文件（导出即报错）。

这个脚本的做法：
  1) template：从工作流 JSON 生成 API prompt 模板 —— 依赖关系从输出节点反向遍历，
     **参考素材相关的输入一律不跟随上游**（ref_images.* / ref_audios.* / ...），
     所以模板里天然没有参考图，也没有 ref_subject_action.png 这类文件依赖；
  2) run：上传你这次给的图/音频 → 按张数注入 ref_images.ref_image_0..N →
     调 /prompt → 轮询 /history → 下载产物。

用法：
  python3 bridge/h3_api.py template --workflow "pika_h3_二采放大_多参考版.json" --out pika_h3_stage1.api.json
  python3 bridge/h3_api.py validate --template pika_h3_stage1.api.json
  python3 bridge/h3_api.py run --template pika_h3_stage1.api.json \\
      --ref a.png --ref b.png --audio voice.wav \\
      --prompt-file prompt.txt --duration 8 --out ./out [--dry-run]

只用标准库，Termux 里可直接跑。
"""

import argparse
import copy
import json
import mimetypes
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE = "http://127.0.0.1:8188"
REF_PREFIXES = ("ref_images.", "ref_audios.", "ref_videos.", "ref_video_audios.")
REF2VA = "MiniMaxH3ReferenceToVideo"


# ---------------------------------------------------------------- ComfyUI 客户端
class ComfyError(RuntimeError):
    """ComfyUI 返回错误或不可达（不用 SystemExit，避免把调用方线程打死）。"""


class Comfy:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self._oi = {}

    def _req(self, path, data=None, headers=None, method=None, timeout=120):
        url = self.base + path
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                msg = json.dumps(parsed, ensure_ascii=False, indent=1)
            except Exception:
                msg = detail
            raise ComfyError(f"HTTP {e.code} {path}\n{msg[:4000]}")
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise ComfyError(f"{path} 返回的不是 JSON（实例可能未就绪）：{body[:200]!r}")

    def object_info(self, cls):
        if cls not in self._oi:
            self._oi[cls] = self._req(f"/object_info/{urllib.parse.quote(cls)}").get(cls, {})
        return self._oi[cls]

    def upload(self, path):
        """上传到 input 目录，返回服务器上的文件名。音频也走 /upload/image。"""
        p = pathlib.Path(path)
        boundary = "----h3api" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        parts = []
        def field(name, value):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode())
            parts.append(str(value).encode())
            parts.append(b"\r\n")
        with open(p, "rb") as f:
            data = f.read()
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; "
             f"name=\"image\"; filename=\"{p.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode())
        parts.append(data)
        parts.append(b"\r\n")
        field("type", "input")
        field("overwrite", "true")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        resp = self._req(
            "/upload/image", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST", timeout=300)
        return resp.get("name", p.name)

    def submit(self, prompt):
        body = json.dumps({"prompt": prompt, "client_id": uuid.uuid4().hex}).encode()
        return self._req("/prompt", data=body,
                         headers={"Content-Type": "application/json"}, method="POST")

    def history(self, prompt_id):
        return self._req(f"/history/{prompt_id}", timeout=60)

    def download(self, item, out_dir):
        q = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        with urllib.request.urlopen(self.base + "/view?" + q, timeout=600) as r:
            blob = r.read()
        dest = pathlib.Path(out_dir) / item["filename"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        return dest


# ---------------------------------------------------------------- 工作流 → 模板
def _norm_links(links):
    """工作流连线统一成 [id, origin, origin_slot, target, target_slot, type]，id 全用字符串。"""
    out = {}
    for l in links:
        if isinstance(l, list):
            lid, o, os_, t, ts, ty = l
        else:
            lid, o, os_ = l["id"], l["origin_id"], l["origin_slot"]
            t, ts, ty = l["target_id"], l["target_slot"], l["type"]
        out[lid] = [lid, str(o), os_, str(t), ts, ty]
    return out


def flatten_subgraphs(wf):
    """把子图实例（type = 子图 uuid）展开成扁平节点，返回 (nodes, links)。

    ComfyUI 的 API prompt 里没有"子图节点"，前端提交前会把子图摊平，
    这里做同样的事：内层节点 id 变成 "<实例id>:<内层id>"。
    """
    nodes = {str(n["id"]): copy.deepcopy(n) for n in wf["nodes"]}
    links = _norm_links(wf["links"])
    subs = {s["id"]: s for s in ((wf.get("definitions") or {}).get("subgraphs") or [])}
    if not subs:
        return nodes, links

    next_lid = (max(links) + 1) if links else 1
    for _ in range(6):
        instances = [nid for nid, n in nodes.items() if n["type"] in subs]
        if not instances:
            break
        for nid in instances:
            outer = nodes.pop(nid)
            sg = subs[outer["type"]]
            prefix = nid + ":"

            def outer_value(k):
                inp = outer["inputs"][k]
                lid = inp.get("link")
                if lid is not None and lid in links:
                    l = links[lid]
                    return ("link", l[1], l[2])
                named = outer.get("widgets_values_named") or {}
                name = inp.get("name")
                if name in named:
                    return ("const", named[name], None)
                widx = [i for i, x in enumerate(outer["inputs"]) if "widget" in x]
                vals = outer.get("widgets_values") or []
                if k in widx and widx.index(k) < len(vals):
                    return ("const", vals[widx.index(k)], None)
                return ("const", None, None)

            inner = {x["id"]: copy.deepcopy(x) for x in sg["nodes"]}
            il = _norm_links(sg["links"])
            # 内层 link id 可能与主图冲突，统一重新编号，并同步内层节点 inputs
            idmap, remapped = {}, {}
            for old, l in il.items():
                idmap[old] = next_lid
                remapped[next_lid] = [next_lid, l[1], l[2], l[3], l[4], l[5]]
                next_lid += 1
            for node in inner.values():
                node["id"] = prefix + str(node["id"])
                for inp in node.get("inputs") or []:
                    if inp.get("link") in idmap:
                        inp["link"] = idmap[inp["link"]]
                nodes[node["id"]] = node

            out_map = {}
            for l in remapped.values():
                lid, o, os_, t, ts, ty = l
                if o == "-10":                      # 子图输入
                    kind, a, b = outer_value(os_)
                    tn = nodes[prefix + t]
                    if kind == "link":
                        links[lid] = [lid, a, b, prefix + t, ts, ty]
                    else:                           # 外层是 widget 常量 → 写进内层节点
                        wname = tn["inputs"][ts]["name"]
                        tn.setdefault("widgets_values_named", {})[wname] = a
                        tn["inputs"][ts]["link"] = None
                elif t == "-20":                    # 子图输出
                    out_map[ts] = (prefix + o, os_)
                else:
                    links[lid] = [lid, prefix + o, os_, prefix + t, ts, ty]

            for l in links.values():
                if l[1] == nid:                     # 外层消费者改接到内层输出
                    m = out_map.get(l[2])
                    if m:
                        l[1], l[2] = m
    return nodes, links


BLOCK_WIDGETS = {"control_after_generate", "upload"}


def build_template(wf, output_ids, comfy):
    nodes, links = flatten_subgraphs(wf)
    keep = set()
    stack = [str(o) for o in output_ids]

    # 反向遍历依赖；参考素材输入不跟随上游（模板里不含任何参考图/音频）
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        n = nodes.get(nid)
        if n is None or n.get("mode") in (2, 4):
            continue
        keep.add(nid)
        for inp in n.get("inputs", []):
            if inp.get("name", "").startswith(REF_PREFIXES):
                continue
            lid = inp.get("link")
            if lid is None:
                continue
            l = links.get(lid)
            if l:
                stack.append(l[1])

    prompt = {}
    for nid in keep:
        n = nodes[nid]
        cls = n["type"]
        if not comfy.object_info(cls):
            raise SystemExit(
                f"节点 #{nid} 类型 {cls!r} 不能直接进 API prompt（可能是未展开的子图）。")
        vals = {}
        for k, v in (n.get("widgets_values_named") or {}).items():
            if k not in BLOCK_WIDGETS:
                vals[k] = v
        # 连线覆盖同名输入；指向未保留节点的输入直接丢弃（等价前端 Remove inputs…）
        for inp in n.get("inputs", []):
            lid = inp.get("link")
            if lid is None:
                continue
            l = links.get(lid)
            if l and l[1] in keep:
                vals[inp["name"]] = [l[1], l[2]]
        prompt[nid] = {"class_type": cls, "inputs": vals,
                       "_meta": {"title": n.get("title") or cls}}
    return prompt


DYNAMIC_TYPES = {"COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3", "COMFY_DYNAMICSLOT_V3"}


def _combo_options(spec_entry):
    """从 object_info 的一条输入定义里取出候选值（不是 combo 就返回 None）。"""
    if not isinstance(spec_entry, list) or not spec_entry:
        return None
    if isinstance(spec_entry[0], list):
        return spec_entry[0]
    if len(spec_entry) > 1 and isinstance(spec_entry[1], dict):
        return spec_entry[1].get("options")
    return None


def validate(prompt, comfy, ignore_combo_ids=frozenset()):
    issues = []
    for nid, item in prompt.items():
        cls = item["class_type"]
        spec = comfy.object_info(cls)
        if not spec:
            issues.append(f"#{nid} 未知节点类型 {cls}")
            continue
        ins = item["inputs"]
        itype = spec.get("input", {})
        for name, s in itype.get("required", {}).items():
            if isinstance(s, list) and s and isinstance(s[0], str) and s[0] in DYNAMIC_TYPES:
                continue  # 动态容器由展开后的 .name 子输入承担
            if name not in ins:
                issues.append(f"#{nid} {cls} 缺必填输入 {name}")
        for cat in ("required", "optional"):
            for name, s in itype.get(cat, {}).items():
                if name not in ins:
                    continue
                if isinstance(s, list) and s and isinstance(s[0], str) and s[0] in DYNAMIC_TYPES:
                    continue  # 动态 combo 的候选由运行时决定，交给服务端校验
                if nid in ignore_combo_ids:
                    continue  # 本次刚上传的文件，object_info 未必已刷新
                opts = _combo_options(s)
                if opts is not None and ins[name] not in opts:
                    issues.append(f"#{nid} {cls}.{name} 值不在候选内: {ins[name]!r}")
        for name, v in ins.items():
            if not (isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)):
                continue
            if v[0] in prompt:
                outs = comfy.object_info(prompt[v[0]]["class_type"]).get("output") or []
                if v[1] >= len(outs):
                    issues.append(f"#{nid}.{name} 源槽越界 {v[0]}[{v[1]}]（{len(outs)} 个输出）")
            elif v[0].isdigit():
                issues.append(f"#{nid}.{name} 指向不存在的节点 {v[0]}")
    return issues


# ---------------------------------------------------------------- 运行
def inject_refs(prompt, refs, audios, videos=(), ref_image_size=None):
    """清空模板里残留的 ref_* 输入，按本次参数注入。"""
    targets = [k for k, v in prompt.items() if v["class_type"] == REF2VA]
    if not targets:
        raise SystemExit("模板里没有 MiniMaxH3ReferenceToVideo 节点")
    for t in targets:
        inputs = prompt[t]["inputs"]
        for k in list(inputs):
            if k.startswith(REF_PREFIXES):
                del inputs[k]

    for i, fname in enumerate(refs):
        nid = f"refimg{i}"
        prompt[nid] = {"class_type": "LoadImage", "inputs": {"image": fname},
                       "_meta": {"title": f"API 参考图 {i+1}"}}
        for t in targets:
            prompt[t]["inputs"][f"ref_images.ref_image_{i}"] = [nid, 0]

    for i, fname in enumerate(audios):
        nid = f"refaud{i}"
        prompt[nid] = {"class_type": "LoadAudio", "inputs": {"audio": fname},
                       "_meta": {"title": f"API 参考音频 {i+1}"}}
        for t in targets:
            prompt[t]["inputs"][f"ref_audios.ref_audio_{i}"] = [nid, 0]

    for i, fname in enumerate(videos):
        l, comp = f"refvid{i}", f"refvidp{i}"
        prompt[l] = {"class_type": "LoadVideo", "inputs": {"file": fname},
                     "_meta": {"title": f"API 参考视频 {i+1}"}}
        prompt[comp] = {"class_type": "GetVideoComponents", "inputs": {"video": [l, 0]},
                        "_meta": {"title": f"API 参考视频 {i+1} 拆帧"}}
        for t in targets:
            prompt[t]["inputs"][f"ref_videos.ref_video_{i}"] = [comp, 0]
            prompt[t]["inputs"][f"ref_video_audios.ref_video_audio_{i}"] = [comp, 1]

    if ref_image_size:
        for t in targets:
            prompt[t]["inputs"]["ref_image_size"] = ref_image_size
    ids = {f"refimg{i}" for i in range(len(refs))} | {f"refaud{i}" for i in range(len(audios))}
    ids |= {f"refvid{i}" for i in range(len(videos))} | {f"refvidp{i}" for i in range(len(videos))}
    return targets, ids


def _coerce(raw):
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def set_named(prompt, nid, name, value):
    node = prompt.get(str(nid))
    if not node:
        raise SystemExit(f"模板里没有节点 #{nid}")
    node["inputs"][name] = value


def cmd_template(a):
    wf = json.loads(pathlib.Path(a.workflow).read_text(encoding="utf-8"))
    comfy = Comfy(a.base)
    prompt = build_template(wf, a.output, comfy)
    issues = validate(prompt, comfy)
    pathlib.Path(a.out).write_text(json.dumps(prompt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"模板已写出：{a.out}  节点 {len(prompt)} 个")
    print("包含节点：", ", ".join(f"#{k}" for k in sorted(prompt, key=lambda x: int(x) if x.isdigit() else 0)))
    for i in issues:
        print("  !", i)
    return 1 if issues else 0


def cmd_validate(a):
    prompt = json.loads(pathlib.Path(a.template).read_text(encoding="utf-8"))
    comfy = Comfy(a.base)
    issues = validate(prompt, comfy)
    print(f"节点 {len(prompt)} 个；问题 {len(issues)} 条")
    for i in issues:
        print("  !", i)
    return 1 if issues else 0


def cmd_run(a):
    comfy = Comfy(a.base)
    prompt = json.loads(pathlib.Path(a.template).read_text(encoding="utf-8"))

    refs = [comfy.upload(p) for p in (a.ref or [])] if not a.dry_run else [pathlib.Path(p).name for p in (a.ref or [])]
    auds = [comfy.upload(p) for p in (a.audio or [])] if not a.dry_run else [pathlib.Path(p).name for p in (a.audio or [])]
    vids = [comfy.upload(p) for p in (a.video or [])] if not a.dry_run else [pathlib.Path(p).name for p in (a.video or [])]
    if len(refs) > 9:
        raise SystemExit("参考图最多 9 张")
    if len(auds) > 3:
        raise SystemExit("参考音频最多 3 条")
    if len(vids) > 3:
        raise SystemExit("参考视频最多 3 段")

    _, injected = inject_refs(prompt, refs, auds, vids, a.ref_image_size)

    if a.prompt_text is not None:
        set_named(prompt, 220, "value", a.prompt_text)
    if a.duration is not None:
        set_named(prompt, 167, "value", a.duration)
    if a.width is not None:
        set_named(prompt, 201, "value", a.width)
    if a.height is not None:
        set_named(prompt, 202, "value", a.height)
    if a.seed is not None:
        set_named(prompt, 400, "value", a.seed)
    for kv in (a.set or []):
        key, _, raw = kv.partition("=")
        nid, _, name = key.partition(".")
        if not (nid and name):
            raise SystemExit(f"--set 需要 NODE.INPUT=VALUE，收到 {kv!r}")
        set_named(prompt, nid, name, _coerce(raw))

    issues = validate(prompt, comfy, ignore_combo_ids=injected)
    if issues:
        print("校验未通过：")
        for i in issues:
            print("  !", i)
        if not a.force:
            return 1

    print(f"参考图 {len(refs)} 张：{refs}")
    print(f"参考音频 {len(auds)} 条：{auds}")
    print(f"参考视频 {len(vids)} 段：{vids}")
    if a.dry_run:
        out = pathlib.Path(a.out) / "prompt.dryrun.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(prompt, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"dry-run：已写 {out}（未提交）")
        return 0

    resp = comfy.submit(prompt)
    pid = resp.get("prompt_id")
    if not pid:
        print("提交失败：", json.dumps(resp, ensure_ascii=False)[:2000])
        return 1
    print(f"已提交 prompt_id={pid}  队列号={resp.get('number')}")

    t0 = time.time()
    while True:
        h = comfy.history(pid)
        if pid in h:
            entry = h[pid]
            status = entry.get("status") or {}
            if status.get("status_str") == "error" or status.get("completed") is False and status.get("messages"):
                pass
            if entry.get("outputs"):
                break
            if status.get("status_str") in ("success", "error"):
                break
        if time.time() - t0 > a.timeout:
            raise SystemExit(f"等待超时（{a.timeout}s），prompt_id={pid} 仍在跑，可稍后用 --prompt-id 查看")
        print(f"  … {int(time.time()-t0)}s", end="\r", flush=True)
        time.sleep(a.interval)

    print()
    entry = h[pid]
    files = []
    seen = set()
    for node_out in (entry.get("outputs") or {}).values():
        for v in node_out.values():
            if not isinstance(v, list):
                continue
            for item in v:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                # 只下正式产物：input 里的是我们刚上传的素材，temp 里的是预览图
                if item.get("type", "output") != "output":
                    continue
                key = (item["filename"], item.get("subfolder", ""))
                if key in seen:
                    continue
                seen.add(key)
                files.append(comfy.download(item, a.out))
    print(f"完成，用时 {int(time.time()-t0)}s，下载 {len(files)} 个文件：")
    for f in files:
        print("  ", f)
    return 0 if files else 0


def main():
    ap = argparse.ArgumentParser(description="pika_h3 工作流的 API 模板与参数化调用")
    ap.add_argument("--base", default=os.environ.get("COMFY_BASE", DEFAULT_BASE))
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("template", help="从工作流生成 API 模板")
    t.add_argument("--workflow", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--output", type=int, nargs="+", default=[193], help="输出节点 id（默认 193=一阶段）")
    t.set_defaults(func=cmd_template)

    v = sub.add_parser("validate", help="本地校验模板")
    v.add_argument("--template", required=True)
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="上传素材并调用生成")
    r.add_argument("--template", required=True)
    r.add_argument("--ref", action="append", help="参考图路径，可重复，0-9 张")
    r.add_argument("--audio", action="append", help="参考音频路径，可重复，0-3 条")
    r.add_argument("--video", action="append", help="参考视频路径，可重复，0-3 段（建议 24fps / 2-15s）")
    r.add_argument("--prompt-text")
    r.add_argument("--prompt-file")
    r.add_argument("--duration", type=float, help="秒数（写 #167）")
    r.add_argument("--width", type=int)
    r.add_argument("--height", type=int)
    r.add_argument("--seed", type=int)
    r.add_argument("--ref-image-size", choices=["match", "max"])
    r.add_argument("--set", action="append", metavar="NODE.INPUT=VALUE",
                   help="临时覆盖任意节点输入，如 --set 410.value=false 关掉二阶段")
    r.add_argument("--out", default="./out")
    r.add_argument("--interval", type=int, default=10)
    r.add_argument("--timeout", type=int, default=2400)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--force", action="store_true", help="校验有问题也提交")
    r.set_defaults(func=cmd_run)

    a = ap.parse_args()
    if getattr(a, "prompt_file", None) and not getattr(a, "prompt_text", None):
        a.prompt_text = pathlib.Path(a.prompt_file).read_text(encoding="utf-8")
    return a.func(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ComfyError as e:
        print(f"[comfy 错误] {e}", file=sys.stderr)
        sys.exit(2)
