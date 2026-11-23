# dsh-minimaxh3-bridge

把 **MiniMax H3 v2 视频生成协议**桥接到**自建 ComfyUI** 上的 `pika_h3`（`MiniMaxH3ReferenceToVideo`）工作流，以 **DeepSeek Harness（DSH）插件**形式交付。

桥接服务是一个**纯标准库 Python 子进程**：DSH 启动时拉起、DSH 退出时停止、异常退出按 1s → 2s → 4s … 退避重启（最多 5 次）。ComfyUI 地址、监听端口、对外 URL、鉴权全部是插件配置 —— 换显卡机器不用改代码。

> 仓库名 `dsh-minimaxh3-bridge`；包名保持 `dsh-minimax-bridge`（与已安装的 profile 依赖名一致）。
> English: a DeepSeek Harness plugin that runs a MiniMax-H3-v2-compatible HTTP bridge in front of a self-hosted ComfyUI `pika_h3` reference-to-video workflow.

---

## 特性

- **协议对齐 MiniMax H3 v2**：创建任务 / 查询 / 列表 / 取消 / 产物下载，支持 `response_format=flat`、`callback_url`、Bearer 鉴权
- **多模态参考素材**：参考图 ≤ 9、参考视频 ≤ 3、参考音频 ≤ 3；URL 支持 http(s)、`data:` base64、本地文件路径
- **两档分辨率**：`768P` 直出；`2K` 默认走「先 768P 生成 → ×2 细化」的二阶段，可用 `two_stage` 覆盖
- **尺寸可控**：`resolution × ratio` 固定映射表之外，还可用 `width/height` 或 `size: "WxH"` 指定第一阶段尺寸（按 32 取整）
- **DSH 原生集成**：Web GUI 设置页、`minimax_bridge` 工具（`status` / `restart` / `logs` / `start` / `stop`）、改配置自动重启子进程
- **任务可恢复**：任务记录与产物落盘（`index.json` + `jobs/` + `outputs/`），重启后仍可查询；中断的 running 标记失败、排队中的任务重新入队
- **零第三方依赖**：Python 侧只用标准库，Termux / 任意 Linux 直接跑；`ffmpeg` 仅在 `generate_audio=false` 去音轨时需要

## 架构

```text
客户端 / 上层应用                          DSH Host 进程
┌────────────────────┐   MiniMax H3 v2    ┌──────────────────────────────────────┐
│ curl / SDK / Agent │ ─────────────────▶ │ index.js（插件入口）                  │
└────────────────────┘   :8099 (HTTP)      │  · 配置 / 设置页 / minimax_bridge 工具│
                                           │  · 托管 Python 子进程（退避重启）     │
                                           └───────────────┬──────────────────────┘
                                                           │ spawn
                                           ┌───────────────▼──────────────────────┐
                                           │ bridge/minimax_bridge.py（标准库）    │
                                           │  · HTTP API + 任务队列 + 状态机       │
                                           └───────────────┬──────────────────────┘
                                                           │ /prompt → /history → /view
                                           ┌───────────────▼──────────────────────┐
                                           │ ComfyUI + pika_h3 Ref2VA 工作流        │
                                           └──────────────────────────────────────┘
```

## 运行环境

| 组件 | 要求 | 说明 |
|---|---|---|
| DeepSeek Harness | 能加载 bundle 插件的版本（`dsh` RC 系列） | 插件 peer 依赖：`@deepseek-ai/cordis ~4.1`、`schemastery ~3.19`、`dsh-tools` |
| Node.js | **≥ 22** | 与 DSH 宿主一致；`index.js` / `client.js` 用 ESM + 原生 `fetch` |
| Python | **≥ 3.9**，仅标准库 | 不需要 `pip install`；Termux 用 `python3` |
| ComfyUI | 已验证 **0.45.1**（前端 1.58.5） | 需装好 `pika_h3` 视频工作流（节点 `MiniMaxH3ReferenceToVideo`） |
| 网络 | 桥接 → ComfyUI 可达；客户端 → `publicBase` 可达 | 手机调本机时把 `publicBase` 填成局域网 IP |
| ffmpeg | 可选 | `generate_audio=false` 时去音轨；缺失则保留音轨并在日志说明 |

一条命令自检运行环境（Node / Python / 模板 / 健康检查）：

```bash
sh scripts/doctor.sh
```

## 安装

```bash
# 1) 克隆到任意目录
git clone git@github.com:MasterZJJ/dsh-minimaxh3-bridge.git
cd dsh-minimaxh3-bridge

# 2) 加入 profile 依赖（dsh plugin 会把参数转发给 profile 目录里的 pnpm，
#    改写 $DSH_HOME/profiles/<profile>/package.json 的 dependencies）
dsh plugin --profile web add link:$PWD
```

```bash
# 3) 把包名追加进 profile 的 bundle 列表（一次即可，手动编辑）
#    $DSH_HOME/profiles/web/package.json → dsh.profile.bundles: [..., "dsh-minimax-bridge"]

# 4) 重启 DSH Web（Termux + runit 环境）
sv restart dsh-web
```

启动后设置页会出现「MiniMax H3 桥接服务」卡片；也可以直接验证：

```bash
curl -s http://127.0.0.1:8099/health
# {"status":"ok","base":"http://127.0.0.1:8188"}
```

> 首次使用请把 **工作流端（`comfyBase`）** 改成你自己的 ComfyUI 地址：设置页修改，或编辑
> `$DSH_HOME/profiles/web/cordis.patch.yml` 里 `id: minimax-bridge` 的 `config.comfyBase`。
> 端口 8099 被占用时同样在设置页改 `port`（改完子进程会自动重启）。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否随 DSH 启动子进程；关闭后只保留工具 |
| `comfyBase` | `http://127.0.0.1:8188` | 工作流执行端（ComfyUI）地址，换显卡机器只改这里 |
| `host` | `0.0.0.0` | 监听地址；`0.0.0.0` 局域网可访问，`127.0.0.1` 仅本机 |
| `port` | `8099` | 桥接监听端口 |
| `publicBase` | 空（→ `http://127.0.0.1:<port>`） | `task.content.url` 的前缀，客户端下载产物用 |
| `apiKey` | 空 | Bearer 鉴权；留空不校验 |
| `workdir` | 空（→ `~/.dsh-minimax-bridge`） | 任务记录与产物目录 |
| `pythonPath` | `python3` | Python 解释器 |
| `scriptPath` | 空（→ 插件内置 `bridge/minimax_bridge.py`） | 指向工作副本可改完即生效 |
| `templateOneStage` | 空（→ 内置模板） | 一阶段 API 模板路径 |
| `templateTwoStage` | 空（→ 内置模板） | 二阶段 API 模板路径 |

改配置后插件会重新应用并重启子进程。设置页背后是同源接口 `GET/POST /minimax-bridge/config`（仅 loopback/同源可写，写入走 DSH Settings 服务的 `mutate`）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v2/video_generation` | 创建任务 → `{"task_id": "...", "base_resp": {...}}` |
| `GET` | `/v2/query/video_generation/{task_id}` | 查询任务；`?response_format=flat` 摊平（默认 `{"task": {...}}`） |
| `GET` | `/v2/query/video_generation?limit=50` | 任务列表 `{"items": [...], "total": n}` |
| `DELETE` | `/v2/video_generation/{task_id}` | 取消 / 删除任务 |
| `GET` | `/v2/files/{task_id}.mp4` | 下载产物（任务 `succeeded` 后可用） |
| `GET` | `/health` | `{"status": "ok", "base": "<comfyBase>"}` |

配置了 `apiKey` 时，所有请求需带 `Authorization: Bearer <apiKey>`；错误返回统一为
`{"type": "error", "error": {"type": "...", "message": "...", "http_code": "..."}, "request_id": "..."}`。

### 创建任务

```bash
curl -X POST http://127.0.0.1:8099/v2/video_generation \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{
  "model": "MiniMax-H3",
  "content": [
    {"type": "text", "text": "雨夜霓虹街头，女孩回头，电影感"},
    {"type": "image_url", "image_url": {"url": "a.png"}, "role": "reference_image"},
    {"type": "image_url", "image_url": {"url": "b.png"}, "role": "reference_image"},
    {"type": "video_url", "video_url": {"url": "motion.mp4"}, "role": "reference_video"},
    {"type": "audio_url", "audio_url": {"url": "voice.wav"}, "role": "reference_audio"}
  ],
  "resolution": "768P",
  "duration": 5,
  "ratio": "16:9",
  "generate_audio": true
}'
```

| 字段 | 可选值 / 说明 |
|---|---|
| `model` | `MiniMax-H3` / `MiniMax-H3-Highspeed` / `MiniMax-H3-Lite` / `MiniMax-H3-Max` |
| `content` | 至少一条非空 `text`；素材项角色仅 `reference_image` / `reference_video` / `reference_audio` |
| `resolution` | `768P` / `2K`（默认 `768P`） |
| `duration` | 秒；1–20（官方 4–15），按工作流 `17n+5` 帧数规则对齐 |
| `ratio` | `adaptive` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16` / `21:9` |
| `generate_audio` | `false` 时产物去音轨（需要 ffmpeg） |
| `callback_url` | 任务状态变更时 POST 同一个 task 体 |
| `response_format` | `task`（默认）/ `flat` |
| `two_stage` | 扩展：`true/false` 覆盖二阶段（2K 默认开、768P 默认关） |
| `ref_image_size` | 扩展：`match` / `max`（模板为空时强制回 `match`） |
| `seed` | 扩展：整数随机种子 |
| `width` + `height` / `size` | 扩展：`"WxH"` 直接指定第一阶段尺寸，按 32 取整 |

### 查询与下载

```bash
curl "http://127.0.0.1:8099/v2/query/video_generation/<task_id>?response_format=flat"
curl -o out.mp4 "http://127.0.0.1:8099/v2/files/<task_id>.mp4"
```

任务状态：`queued` / `running` / `succeeded` / `failed` / `cancelled`；`running` 时带 `progress`（按时长估算的百分比），完成后 `content.url` 指向 `publicBase/v2/files/<id>.mp4`。

## 尺寸表

`resolution × ratio` 映射到固定尺寸（均为 32 的倍数；2K = 768P × 2）：

| ratio | 768P | 2K |
|---|---|---|
| 16:9 | 1344×768 | 2688×1536 |
| 4:3 | 1024×768 | 2048×1536 |
| 1:1 | 768×768 | 1536×1536 |
| 3:4 | 768×1024 | 1536×2048 |
| 9:16 | 768×1344 | 1536×2688 |
| 21:9 | 1792×768 | 3584×1536 |

`resolution=2K` 默认二阶段（先 768P 生成再 ×2 细化）；自定义 `width/height` / `size` 给的是**第一阶段**尺寸，二阶段时最终仍 ×2。

## DSH 内使用

- **设置页**：「MiniMax H3 桥接服务」卡片可改上面全部配置项
- **工具** `minimax_bridge`：
  - `status` — 子进程 PID、监听地址、目标 ComfyUI、公开地址、实时 `/health`
  - `restart` — 换地址后重启子进程
  - `logs` — 最近输出（含 Python 侧报错）
  - `start` / `stop` — 只动子进程，不卸载插件

## 目录结构

```text
dsh-minimaxh3-bridge/
├── index.js                  # Host 插件：配置模式 / 子进程托管 / 工具 / 配置路由
├── client.js                 # Web GUI 设置页卡片
├── cordis.patch.yml          # bundle 默认行（端口 8099、默认 comfyBase）
├── package.json
├── requirements.txt          # 说明：Python 侧零第三方依赖
├── locale/{zh,en}.json       # 设置页文案
├── bridge/
│   ├── minimax_bridge.py     # 桥接 HTTP 服务（纯标准库）
│   ├── h3_api.py             # 模板编译 / 校验 / 直连联调 CLI
│   └── templates/            # 编译好的 pika_h3 API 模板（一阶段 / 二阶段）
├── docs/DETAIL.md            # 实现细节、状态机、模板注入、故障排查
└── scripts/
    ├── doctor.sh             # 运行环境自检
    └── smoke.sh              # 在线冒烟（健康 + 任务列表）
```

## 开发

```bash
# 语法检查（CI 同款）
node --check index.js && node --check client.js
python3 -m py_compile bridge/minimax_bridge.py bridge/h3_api.py

# 不经过 DSH 直接起桥接（调试用）
python3 bridge/minimax_bridge.py --base http://127.0.0.1:8188 --port 8199 --workdir ./tmp

# 从 ComfyUI 工作流重新编译模板（换工作流后必做）
python3 bridge/h3_api.py template --workflow pika_h3_二采放大_多参考版.json \
  --out bridge/templates/pika_h3_stage1.api.json
python3 bridge/h3_api.py validate --template bridge/templates/pika_h3_stage1.api.json
```

## 故障排查

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `status` 显示 `unreachable` / 端口无响应 | 端口被占、Python 不存在、脚本路径错 | 看 `minimax_bridge logs`；`ss -ltnp \| grep 8099`；设置页核对 `pythonPath` / `scriptPath` |
| 子进程反复重启后放弃 | 上次残留进程占端口，或模板损坏 | `stop` 后 `pkill -f minimax_bridge.py`，再 `start` |
| 任务一直 `queued` | 队列串行 + 上游慢 | 正常现象，看 `progress`；提高并发需改工作流/机器 |
| `400 ... first_frame` | 请求用了首尾帧角色 | 本插件基于 Ref2VA，只支持 `reference_*`；首尾帧需 FL2VA 模型 |
| ComfyUI 400 且提到节点/输入名 | 模板与当前 ComfyUI 工作流不一致 | 用 `h3_api.py template` 重新编译模板并替换 `bridge/templates/` |
| 2K 很慢 | 二阶段会跑两次工作流 | 可传 `"two_stage": false` 直出 2K，或退回 768P |
| 手机打不开 `content.url` | `publicBase` 还是 127.0.0.1 | 设成局域网 IP（如 `http://192.168.1.8:8099`）后重启 |
| 产物没有声音 | `generate_audio=false` 或本机无 ffmpeg | 检查该字段与 ffmpeg 是否存在 |

更细的内部行为、状态机与模板注入规则见 [docs/DETAIL.md](docs/DETAIL.md)。

## 已知限制

- 只支持 `reference_image` / `reference_video` / `reference_audio`；首尾帧需要 FL2VA 权重
- 参考图 ≤ 9、参考视频 ≤ 3、参考音频 ≤ 3；参考音频不能单独使用
- 任务串行执行（单 worker 队列）；并发由上游队列决定
- 进度是按 `expect_seconds` 估算的百分比，不是采样步数
- 模板随工作流编译而来：换 ComfyUI 工作流/升级节点后需重新编译模板

## 许可证

[MIT](LICENSE)
