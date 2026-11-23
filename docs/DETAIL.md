# 实现细节

本文是 [README](../README.md) 的深入版：生命周期、任务状态机、模板注入、落盘结构、排障路径。

## 1. 组件与生命周期

```text
index.js（DSH Host 插件）
  └─ spawn(pythonPath, bridge/minimax_bridge.py --base ... --port ... --workdir ...)
       └─ ThreadingHTTPServer(:8099)  ──HTTP──▶  ComfyUI(/prompt /history /view)
```

- `apply()` 时按 `enabled` 拉起子进程；插件 dispose（DSH 退出 / 插件被停用）时 `SIGTERM`，2.5s 后 `SIGKILL` 兜底，等到子进程真正退出才返回（保证端口释放）。
- 子进程非预期退出时按 `1s / 2s / 4s / 8s / 16s`（上限 30s）退避重启，**最多 5 次**；之后放弃并记录 `lastError`，可用工具 `start` 重新拉起。
- 子进程 stdout/stderr 进插件内存环形缓冲（保留最近 300 行），`minimax_bridge logs` 取最近 60 行。
- 改配置（设置页或 `cordis.patch.yml` 热更新）会触发 `loader/volatile-update`：停子进程 → 用新配置重启，`restarts` 归零。
- `pythonPath` / `scriptPath` / 模板路径都可在配置里覆盖；默认用插件内置副本（`bridge/…`）。

## 2. 任务状态机

```text
POST /v2/video_generation
        │  落盘素材 → 入队
        ▼
     queued ──worker 取出──▶ in_progress ──ComfyUI 完成──▶ completed
        │                        │
        │ DELETE                 ├─ ComfyUI 报错/超时 ──▶ failed
        └──── DELETE ───────────▶ cancelled
```

- 单 worker 串行消费队列（`queue` + `Condition`），保证 RunningHub/GPU 侧不并发抢占。
- 对外状态映射：`queued`→`queued`、`in_progress`→`running`、`completed`→`succeeded`、`failed`→`failed`、`cancelled`→`cancelled`。
- `running` 时 `progress` 按 `expect_seconds`（一阶段 90s、二阶段 240s）线性估算，封顶 95%，完成置 100。
- 重启恢复：启动时把所有 `in_progress` 置为 `failed`（`error.type=interrupted`），仍 `queued` 的重新入队。
- 超时：`--timeout`（默认 3600s）为单任务最长等待，超时判失败并释放 worker。

## 3. 参考素材注入

创建任务时按 `content[]` 分桶，下载/解码后落到 `workdir/jobs/<task_id>/`，再注入模板：

| 协议字段 | 模板输入前缀 | 上限 |
|---|---|---|
| `image_url`（`reference_image`） | `ref_images.ref_image_N` | 9 |
| `video_url`（`reference_video`） | `ref_videos.*` / `ref_video_audios.*` | 3 / 3 |
| `audio_url`（`reference_audio`） | `ref_audios.*` | 3 |

规则：

- 至少一条非空 `text`（拼成 `prompt`）；至少一张图或一段视频（参考音频不能单独使用）。
- 素材 URL 支持三种写法：`http(s)://`（直接下载）、`data:image/...;base64,...`（解码）、本地绝对/相对路径（拷贝）。
- 模板里参考素材相关的输入**不跟随上游**，所以模板本身是“无素材”的空壳，槽位按本次请求的张数动态填充；未用到的槽位删除，避免空文件报错。
- 模板中 `ref_image_size` 若为空，桥接会强制回 `match`（为空时 H3 按 2048 短边跑，慢数倍）。

## 4. 尺寸与二阶段

`resolution × ratio` 映射到固定尺寸表（见 README）。二阶段逻辑：

1. 第一阶段按 `768P`（或自定义宽高，按 32 取整）生成；
2. 把产物作为第二阶段工作流的输入，`×2` 细化到 2K 尺寸；
3. `two_stage` 未显式给出时：`resolution=2K` → 开，`768P` → 关；
4. 任务体里的 `size` 是**最终输出尺寸**，`gen_size`（落盘记录）是第一阶段的生成尺寸。

## 5. 回调

请求带 `callback_url` 时，任务在 `succeeded` / `failed` / `cancelled` 状态变更后向该地址 POST
`{"task": {...task_body...}}`（后台线程发送，失败只记日志，不阻塞任务）。回调体始终带 `callback_url` 的任务快照。

## 6. 落盘结构

```text
workdir/                         # 默认 ~/.dsh-minimax-bridge
├── index.json                   # 任务索引（原子写）
├── jobs/<task_id>/              # 本次请求的参考素材（images_N / audios_N / videos_N）
└── outputs/<task_id>/...mp4     # 产物
```

`GET /v2/files/<task_id>.mp4` 直接读 `job["file"]`；任务记录 `completed` 后即使桥接重启也能下载。

## 7. 模板从哪来

`bridge/templates/*.api.json` 由 `bridge/h3_api.py template` 从 ComfyUI 工作流 JSON 编译：

- 从输出节点反向遍历依赖，遇到参考素材输入（`ref_images.*`、`ref_audios.*`、`ref_videos.*`、`ref_video_audios.*`）一律不跟随上游；
- 因此模板天然不含固定图片路径，运行时再按张数注入；
- 换工作流（改节点、改模型、改尺寸参数）后必须重新编译，否则 ComfyUI 会返回“输入名不存在”。

```bash
python3 bridge/h3_api.py template --workflow pika_h3_二采放大_多参考版.json \
  --out bridge/templates/pika_h3_stage1.api.json
python3 bridge/h3_api.py validate --template bridge/templates/pika_h3_stage1.api.json
```

`h3_api.py run` 可绕过桥接直接联调工作流（上传素材 → `/prompt` → 轮询 → 下载），排障时很有用。

## 8. 已验证环境

| 项 | 版本 |
|---|---|
| ComfyUI | 0.45.1（前端 1.58.5） |
| 工作流节点 | `MiniMaxH3ReferenceToVideo`：`ref_images` ≤ 9、`ref_videos` ≤ 3、`ref_video_audios` ≤ 3、`ref_audios` ≤ 3 |
| Python | 3.9+（开发机 3.14 验证），仅标准库 |
| Node.js | ≥ 22（DSH 宿主要求） |
| 平台 | Termux (Android/aarch64)、Linux |

## 9. 排障

1. **先看进程**：`minimax_bridge status` / `minimax_bridge logs`；Python 侧异常都会打在 stderr。
2. **再看端口**：`ss -ltnp | grep <port>`；残留进程用 `pkill -f minimax_bridge.py`。
3. **再看健康**：`curl -s http://127.0.0.1:<port>/health` → `{"status":"ok","base":"<comfyBase>"}`。
4. **单独跑**（脱离 DSH 定位）：
   ```bash
   python3 bridge/minimax_bridge.py --base http://127.0.0.1:8188 --port 8199 --workdir ./tmp
   ```
   `8199` 起得来说明脚本/模板没问题，问题在插件配置或端口。
5. **ComfyUI 报错**：`/prompt` 400 多为模板与工作流不一致；`/history` 里 `messages` 会带节点错误。
6. **客户端下载 404**：任务未 `succeeded`，或 `publicBase` 指向错误主机。
7. **去音轨失败**：`which ffmpeg` 为空时桥接保留音轨并打印提示，不影响出片。

## 10. 限制

- 只支持 `reference_image` / `reference_video` / `reference_audio` 角色；`first_frame` / `last_frame` 需要 FL2VA 权重（当前未安装），会返回明确 400。
- 参考图 ≤ 9、参考视频 ≤ 3、参考音频 ≤ 3；参考音频不能单独使用。
- 单 worker 串行；`duration` 1–20 秒（官方 4–15），按 `17n+5` 帧数对齐。
- 产物固定 mp4；`generate_audio=false` 依赖本机 ffmpeg。
- 进度为估算值；2K 二阶段耗时约为一阶段的两倍以上。
