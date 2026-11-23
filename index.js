import { spawn } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'

const PKG_DIR = fileURLToPath(new URL('.', import.meta.url))
const SCRIPT = join(PKG_DIR, 'bridge', 'minimax_bridge.py')
const TPL_ONE = join(PKG_DIR, 'bridge', 'templates', 'pika_h3_stage1.api.json')
const TPL_TWO = join(PKG_DIR, 'bridge', 'templates', 'pika_h3_2stage.api.json')
const ROUTE = '/minimax-bridge/config'

export const name = 'minimax-bridge'
export const inject = ['webServer', 'tools']

/**
 * Every field is `.volatile()`: the Settings service only projects volatile
 * fields into the config form and refuses a write to any other path, and a
 * volatile field arrives in `apply` as a reference that is updated in place.
 */
export const Config = z.object({
  enabled: z.boolean().default(true).volatile()
    .description('随 DSH 启动桥接服务；关闭则只保留工具但不起进程。'),
  comfyBase: z.string().default('http://127.0.0.1:8188').volatile()
    .description('工作流执行端（ComfyUI）地址；换显卡机器时改这里。'),
  host: z.string().default('0.0.0.0').volatile()
    .description('监听地址。0.0.0.0 = 局域网可访问；127.0.0.1 = 仅本机。'),
  port: z.natural().min(1).max(65535).default(8099).volatile()
    .description('监听端口。'),
  publicBase: z.string().default('').volatile()
    .description('task.content.url 前缀，留空则 http://127.0.0.1:<port>。'),
  apiKey: z.string().default('').volatile()
    .description('Bearer 鉴权；留空不校验。'),
  workdir: z.string().default('').volatile()
    .description('任务与产物目录，留空 = ~/.dsh-minimax-bridge。'),
  pythonPath: z.string().default('python3').volatile()
    .description('Python 解释器。'),
  scriptPath: z.string().default('').volatile()
    .description('桥接脚本路径，留空用插件内置 bridge/minimax_bridge.py。'),
  templateOneStage: z.string().default('').volatile()
    .description('一阶段 API 模板，留空用内置。'),
  templateTwoStage: z.string().default('').volatile()
    .description('二阶段 API 模板，留空用内置。'),
})

const KEYS = ['enabled', 'comfyBase', 'host', 'port', 'publicBase', 'apiKey',
  'workdir', 'pythonPath', 'scriptPath', 'templateOneStage', 'templateTwoStage']

/** Unwrap the volatile references into one plain snapshot. */
function readConfig(refs) {
  return {
    enabled: refs.enabled.get(),
    comfyBase: refs.comfyBase.get(),
    host: refs.host.get(),
    port: refs.port.get(),
    publicBase: refs.publicBase.get(),
    apiKey: refs.apiKey.get(),
    workdir: refs.workdir.get(),
    pythonPath: refs.pythonPath.get(),
    scriptPath: refs.scriptPath.get(),
    templateOneStage: refs.templateOneStage.get(),
    templateTwoStage: refs.templateTwoStage.get(),
  }
}

/** Start/stop the Python bridge process, serve its config form, expose a tool. */
export function apply(ctx, config) {
  let cfg = readConfig(config)
  let child = null
  let disposed = false
  let restarts = 0
  let lastError = ''
  let restartTimer = null
  const logs = []
  let settingsProvider
  let settingsEntryId

  const pushLog = (line) => {
    for (const part of String(line).split('\n')) if (part !== '') logs.push(part)
    while (logs.length > 300) logs.shift()
  }
  const workdirOf = () => cfg.workdir || join(homedir(), '.dsh-minimax-bridge')
  const publicBaseOf = () => cfg.publicBase || `http://127.0.0.1:${cfg.port}`
  const healthUrl = () => `http://127.0.0.1:${cfg.port}/health`

  function argv() {
    const out = [
      cfg.scriptPath || SCRIPT,
      '--base', cfg.comfyBase,
      '--host', cfg.host,
      '--port', String(cfg.port),
      '--public-base', publicBaseOf(),
      '--workdir', workdirOf(),
      '--template-one-stage', cfg.templateOneStage || TPL_ONE,
      '--template-two-stage', cfg.templateTwoStage || TPL_TWO,
    ]
    if (cfg.apiKey) out.push('--api-key', cfg.apiKey)
    return out
  }

  function start() {
    if (disposed || child || !cfg.enabled) return
    const script = cfg.scriptPath || SCRIPT
    if (!existsSync(script)) {
      lastError = `bridge script missing at ${script}`
      ctx.logger.error(`minimax-bridge: ${lastError}`)
      return
    }
    try {
      mkdirSync(workdirOf(), { recursive: true })
      child = spawn(cfg.pythonPath, argv(), { stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      lastError = String(error?.message ?? error)
      ctx.logger.error(`minimax-bridge: spawn failed — ${lastError}`)
      child = null
      return
    }
    child.stdout?.on('data', (d) => pushLog(d.toString()))
    child.stderr?.on('data', (d) => pushLog(d.toString()))
    child.on('error', (error) => {
      lastError = String(error?.message ?? error)
      ctx.logger.error(`minimax-bridge: ${lastError}`)
      child = null
    })
    child.on('exit', (code, signal) => {
      child = null
      if (disposed) return
      lastError = `exited code=${code} signal=${signal}`
      ctx.logger.warn(`minimax-bridge: process ${lastError}`)
      if (restarts >= 5) {
        ctx.logger.error('minimax-bridge: giving up after 5 restarts')
        return
      }
      restarts += 1
      const delay = Math.min(30_000, 1000 * 2 ** (restarts - 1))
      restartTimer = setTimeout(() => {
        restartTimer = null
        start()
      }, delay)
    })
    lastError = ''
    ctx.logger.info(`minimax-bridge: starting on ${cfg.host}:${cfg.port} -> ${cfg.comfyBase}`)
  }

  /** Stop the child and resolve only once it is really gone (port released). */
  async function stop() {
    if (restartTimer) {
      clearTimeout(restartTimer)
      restartTimer = null
    }
    if (!child) return
    const proc = child
    child = null
    await new Promise((resolve) => {
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        resolve()
      }
      proc.once('exit', finish)
      try {
        proc.kill('SIGTERM')
      } catch {
        finish()
        return
      }
      setTimeout(() => {
        try {
          proc.kill('SIGKILL')
        } catch {
          /* already gone */
        }
      }, 2500)
      setTimeout(finish, 3500)
    })
    await new Promise((r) => setTimeout(r, 150))
  }

  async function health() {
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 2000)
      const res = await fetch(healthUrl(), { signal: controller.signal })
      clearTimeout(timer)
      if (!res.ok) return `http ${res.status}`
      const body = await res.json()
      return body?.status === 'ok' ? 'ok' : JSON.stringify(body)
    } catch (error) {
      return `unreachable (${String(error?.message ?? error)})`
    }
  }

  async function snapshot() {
    return {
      config: { ...cfg },
      runtime: {
        running: child !== null,
        pid: child?.pid ?? null,
        listen: `${cfg.host}:${cfg.port}`,
        comfyBase: cfg.comfyBase,
        publicBase: publicBaseOf(),
        workdir: workdirOf(),
        health: await health(),
        lastError: lastError || null,
        settingsWritable: Boolean(settingsProvider && settingsEntryId),
      },
    }
  }

  // ---- config route (same-origin loopback only) -------------------------
  const sameOrigin = (req) => {
    const origin = req.headers.origin
    if (!origin) return true
    try {
      return new URL(origin).host === req.headers.host
    } catch {
      return false
    }
  }
  const sendJson = (res, status, body) => {
    res.writeHead(status, { 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
  }
  const readBody = async (req, res) => {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    try {
      return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
    } catch {
      sendJson(res, 400, { error: 'invalid JSON body' })
      return undefined
    }
  }

  const routeHandler = async (req, res) => {
    if (!sameOrigin(req)) {
      sendJson(res, 403, { error: 'same-origin requests only' })
      return
    }
    if (req.method === 'GET') {
      sendJson(res, 200, await snapshot())
      return
    }
    if (req.method !== 'POST') {
      sendJson(res, 405, { error: 'method not allowed' })
      return
    }
    const body = await readBody(req, res)
    if (body === undefined) return
    if (typeof body !== 'object' || body === null || Array.isArray(body)) {
      sendJson(res, 400, { error: 'body must be a config object' })
      return
    }
    if (!settingsProvider || !settingsEntryId) {
      sendJson(res, 409, {
        error: 'settings service unavailable — edit the profile cordis.patch.yml and restart dsh',
      })
      return
    }
    const patch = body.config ?? body
    const ops = []
    for (const key of KEYS) {
      if (!(key in patch)) continue
      let value = patch[key]
      if (key === 'enabled') value = Boolean(value)
      else if (key === 'port') {
        value = Number(value)
        if (!Number.isInteger(value) || value < 1 || value > 65535) {
          sendJson(res, 400, { error: `port 非法：${patch[key]}` })
          return
        }
      } else value = String(value ?? '')
      if (key === 'comfyBase' && value.trim() === '') {
        sendJson(res, 400, { error: 'comfyBase 不能为空' })
        return
      }
      ops.push({ op: 'set', path: [key], value })
    }
    if (ops.length === 0) {
      sendJson(res, 400, { error: 'no known config keys in body' })
      return
    }
    try {
      await settingsProvider.mutate(settingsEntryId, ops)
    } catch (error) {
      sendJson(res, 409, { error: String(error?.message ?? error) })
      return
    }
    sendJson(res, 200, { ok: true, applied: ops.map((o) => o.path[0]) })
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: ROUTE,
    handler: routeHandler,
  }), 'minimax-bridge: config route')

  // ---- settings attachment + live re-apply ------------------------------
  ctx.inject(['settings'], (sctx) => {
    settingsEntryId = ctx.fiber.entry?.options.id
    settingsProvider = sctx.settings
    if (settingsEntryId === undefined) return
    sctx.effect(() => sctx.settings.configure({ auto: false }, ctx.fiber))
    sctx.effect(() => ctx.on('loader/volatile-update', () => {
      const next = readConfig(config)
      const changed = KEYS.filter((k) => next[k] !== cfg[k])
      const nextCfg = next
      ctx.logger.info(`minimax-bridge: config changed (${changed.join(', ') || 'none'}), restarting child`)
      void (async () => {
        await stop()
        restarts = 0
        cfg = nextCfg
        start()
      })()
    }))
    sctx.effect(() => () => {
      settingsProvider = undefined
      settingsEntryId = undefined
    })
  })

  // ---- boot + dispose ---------------------------------------------------
  start()
  ctx.effect(() => () => {
    disposed = true
    return stop().then(() => ctx.logger.info('minimax-bridge: stopped'))
  }, 'minimax-bridge: python child process')

  // ---- tool -------------------------------------------------------------
  ctx.tools.register(defineTool({
    name: 'minimax_bridge',
    description: 'Manage the MiniMax H3 v2 API bridge this profile runs: `status` reports the child process, listening address, targeted ComfyUI base, public URL and a live /health probe; `restart` replaces the child process; `logs` returns recent bridge output; `stop` / `start` toggle the child without unloading the plugin. Settings (ComfyUI base, port, public URL, API key, template/script paths) are editable on the plugin page.',
    parameters: {
      command: {
        type: 'string',
        enum: ['status', 'restart', 'logs', 'start', 'stop'],
        description: '`status` (default) — process + health. `restart` — stop and spawn again. `logs` — recent output. `start` / `stop` — toggle the child process.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          message: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.message }],
    },
    async execute(args) {
      const command = args.command ?? 'status'
      if (command === 'start') {
        if (child) return { ok: true, message: 'minimax-bridge 已在运行，未重复启动。' }
        restarts = 0
        start()
        return { ok: true, message: `已启动：${cfg.host}:${cfg.port} -> ${cfg.comfyBase}` }
      }
      if (command === 'stop') {
        await stop()
        return { ok: true, message: '已停止子进程（插件仍加载，可 start 再拉起）。' }
      }
      if (command === 'restart') {
        await stop()
        restarts = 0
        await new Promise((r) => setTimeout(r, 300))
        cfg = readConfig(config)
        start()
        await new Promise((r) => setTimeout(r, 1200))
        return { ok: true, message: `已重启。健康检查：${await health()}` }
      }
      if (command === 'logs') {
        return { ok: true, message: logs.slice(-60).join('\n') || '（暂无输出）' }
      }
      const state = child ? `运行中 pid=${child.pid}` : `未运行${lastError ? `（最近：${lastError}）` : ''}`
      return {
        ok: true,
        message: [
          `状态：${state}`,
          `监听：${cfg.host}:${cfg.port}`,
          `工作流端：${cfg.comfyBase}`,
          `公开地址：${publicBaseOf()}`,
          `工作目录：${workdirOf()}`,
          `健康检查：${await health()}`,
        ].join('\n'),
      }
    },
  }))
}
