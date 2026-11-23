window.__ModuleLoader__.load({
  id: 'dsh-minimax-bridge',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const ENTRY_ID = 'minimax-bridge'
    const ENDPOINT = '/minimax-bridge/config'

    const FIELDS = [
      { key: 'enabled', label: '随 DSH 启动', type: 'boolean',
        hint: '关闭后只保留工具，不拉起桥接进程' },
      { key: 'comfyBase', label: '工作流端（ComfyUI 地址）', type: 'text',
        hint: '换显卡机器只改这一项' },
      { key: 'host', label: '监听地址', type: 'text',
        hint: '0.0.0.0 = 局域网可访问；127.0.0.1 = 仅本机' },
      { key: 'port', label: '监听端口', type: 'number', hint: '默认 8099' },
      { key: 'publicBase', label: '对外地址前缀', type: 'text',
        hint: 'task.content.url 用；留空则 http://127.0.0.1:<port>' },
      { key: 'apiKey', label: 'Bearer 鉴权密钥', type: 'password', hint: '留空不校验' },
      { key: 'workdir', label: '工作目录', type: 'text', hint: '留空 = ~/.dsh-minimax-bridge' },
      { key: 'pythonPath', label: 'Python 解释器', type: 'text', hint: '默认 python3' },
      { key: 'scriptPath', label: '桥接脚本路径', type: 'text',
        hint: '留空用插件内置副本；指向工作副本可改完即生效' },
      { key: 'templateOneStage', label: '一阶段模板', type: 'text', hint: '留空用内置' },
      { key: 'templateTwoStage', label: '二阶段模板', type: 'text', hint: '留空用内置' },
    ]

    const styles = {
      wrap: { display: 'flex', flexDirection: 'column', gap: '14px', padding: '4px 0' },
      strip: { fontSize: '12px', lineHeight: 1.7, opacity: 0.8 },
      row: { display: 'flex', flexDirection: 'column', gap: '4px' },
      label: { fontSize: '13px', fontWeight: 600 },
      hint: { fontSize: '12px', opacity: 0.65 },
      input: {
        fontSize: '13px', padding: '7px 9px', borderRadius: '7px',
        border: '1px solid rgba(127,127,127,.45)', background: 'transparent',
        color: 'inherit', width: '100%', boxSizing: 'border-box',
      },
      check: { display: 'flex', alignItems: 'center', gap: '8px' },
      actions: { display: 'flex', alignItems: 'center', gap: '12px', marginTop: '4px' },
      button: {
        fontSize: '13px', padding: '7px 16px', borderRadius: '7px',
        border: '1px solid rgba(127,127,127,.45)', background: 'transparent',
        color: 'inherit', cursor: 'pointer',
      },
      note: { fontSize: '12px', opacity: 0.8 },
      error: { fontSize: '12px', color: '#d9534f' },
    }

    function Card() {
      const [data, setData] = React.useState(null)
      const [draft, setDraft] = React.useState({})
      const [saving, setSaving] = React.useState(false)
      const [message, setMessage] = React.useState('')
      const [error, setError] = React.useState('')

      const load = React.useCallback(async () => {
        try {
          const res = await fetch(ENDPOINT, { headers: { accept: 'application/json' } })
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          const body = await res.json()
          setData(body)
          setDraft(body.config ?? {})
          setError('')
        } catch (e) {
          setError(`读取配置失败：${String(e?.message ?? e)}`)
        }
      }, [])

      React.useEffect(() => { load() }, [load])

      const set = (key, value) => setDraft((prev) => ({ ...prev, [key]: value }))

      const save = async () => {
        setSaving(true)
        setMessage('')
        setError('')
        try {
          const res = await fetch(ENDPOINT, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ config: draft }),
          })
          const body = await res.json().catch(() => ({}))
          if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`)
          setMessage(`已保存（${body.applied?.join(', ') ?? ''}），桥接正在重启…`)
          setTimeout(load, 1800)
        } catch (e) {
          setError(`保存失败：${String(e?.message ?? e)}`)
        } finally {
          setSaving(false)
        }
      }

      const runtime = data?.runtime
      return h('div', { style: styles.wrap },
        runtime
          ? h('div', { style: styles.strip },
              `桥接：${runtime.running ? `运行中 pid=${runtime.pid}` : '未运行'}`,
              ` · 监听 ${runtime.listen}`,
              ` · 健康 ${runtime.health}`,
              h('br'),
              `工作流端 ${runtime.comfyBase}`,
              ` · 对外 ${runtime.publicBase}`,
              ` · 目录 ${runtime.workdir}`,
              runtime.lastError ? h('div', { style: styles.error }, `最近错误：${runtime.lastError}`) : null,
              runtime.settingsWritable === false
                ? h('div', { style: styles.note }, '设置服务未就绪，暂时只能改 profile 的 cordis.patch.yml')
                : null)
          : null,
        FIELDS.map((field) => {
          const value = draft[field.key]
          if (field.type === 'boolean') {
            return h('div', { key: field.key, style: styles.row },
              h('label', { style: styles.check },
                h('input', {
                  type: 'checkbox',
                  checked: Boolean(value),
                  onChange: (e) => set(field.key, e.target.checked),
                }),
                h('span', { style: styles.label }, field.label)),
              h('span', { style: styles.hint }, field.hint))
          }
          return h('div', { key: field.key, style: styles.row },
            h('label', { style: styles.label, htmlFor: `mmb-${field.key}` }, field.label),
            h('input', {
              id: `mmb-${field.key}`,
              style: styles.input,
              type: field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text',
              value: value ?? '',
              onChange: (e) => set(field.key, e.target.value),
            }),
            h('span', { style: styles.hint }, field.hint))
        }),
        h('div', { style: styles.actions },
          h('button', { style: styles.button, onClick: save, disabled: saving },
            saving ? '保存中…' : '保存并重启桥接'),
          h('button', { style: styles.button, onClick: load, disabled: saving }, '重新读取'),
          message ? h('span', { style: styles.note }, message) : null),
        error ? h('div', { style: styles.error }, error) : null)
    }

    return {
      name: 'dsh-minimax-bridge',
      inject: ['slots', 'configForms'],
      apply(ctx) {
        ctx.effect(() => ctx.configForms.whileServed([ENTRY_ID], () =>
          ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
            name: 'plugins.bundle.config',
            key: 'dsh-minimax-bridge',
            inject: () => ({ entryId: ENTRY_ID }),
          }, Card))), 'minimax-bridge: settings card')
      },
    }
  },
})
