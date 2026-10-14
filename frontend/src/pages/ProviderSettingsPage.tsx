import { useRef, useState } from 'react'
import { api, type ProviderConfig, type ProviderAdminResponse, type ProviderDraft } from '../api'

const emptyForm: ProviderDraft = { enabled: true, provider: '', base_url: '', model: '', models: [], gateway_messages: false, api_key: '' }

function messageFor(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback
}

function formFromConfig(config: ProviderConfig): ProviderDraft {
  return { enabled: config.enabled, provider: config.provider, base_url: config.base_url, model: config.model, models: [...config.models], gateway_messages: config.gateway_messages, api_key: '' }
}

export function ProviderSettingsPage() {
  const [adminToken, setAdminToken] = useState('')
  const [registry, setRegistry] = useState<ProviderAdminResponse | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [form, setForm] = useState<ProviderDraft>(emptyForm)
  const [creating, setCreating] = useState(false)
  const [copyFrom, setCopyFrom] = useState<string | undefined>()
  const [models, setModels] = useState<Array<{ id: string }>>([])
  const [modelsBusy, setModelsBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const modelRequest = useRef(0)

  const selected = registry?.providers.find((provider) => provider.id === selectedId) ?? null
  const update = <K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) => {
    if (key === 'base_url' || key === 'api_key') {
      modelRequest.current += 1
      setModelsBusy(false)
      setModels([])
    }
    setForm((current) => ({ ...current, [key]: value }))
  }
  const loadRegistry = async (token = adminToken) => {
    if (!token.trim()) return
    setBusy(true); setError(''); setMessage('')
    try {
      const next = await api.providers.get(token)
      setRegistry(next)
      const initial = next.providers.find((provider) => provider.id === next.default_provider_id) ?? next.providers[0]
      if (initial) { setSelectedId(initial.id); setForm(formFromConfig(initial)); setModels(initial.models.map((id) => ({ id }))); setCreating(false) }
      setMessage('管理配置已解锁。API token 不会从服务器回填。')
    } catch (reason: unknown) { setError(messageFor(reason, '管理口令无效或配置读取失败，请重试。')) }
    finally { setBusy(false) }
  }
  const selectProvider = (id: string) => {
    const next = registry?.providers.find((provider) => provider.id === id)
    if (!next) return
    modelRequest.current += 1
    setModelsBusy(false)
    setSelectedId(id); setCreating(false); setCopyFrom(undefined); setModels(next.models.map((model) => ({ id: model }))); setForm(formFromConfig(next)); setError(''); setMessage('')
  }
  const startNew = (source?: ProviderConfig) => {
    modelRequest.current += 1
    setModelsBusy(false)
    setCreating(true); setSelectedId(''); setCopyFrom(source?.id); setModels(source ? source.models.map((id) => ({ id })) : [])
    setForm(source ? formFromConfig(source) : emptyForm)
    setError(''); setMessage(source ? '已复制公开配置字段；保存时才会明确复制其 API token。' : '')
  }
  const loadModels = async () => {
    const sequence = ++modelRequest.current
    setModelsBusy(true); setError(''); setMessage('')
    try {
      const result = await api.providers.models(adminToken, { ...form, ...(creating ? copyFrom ? { provider_id: copyFrom } : {} : { provider_id: selectedId }), api_key: form.api_key?.trim() || undefined })
      if (sequence !== modelRequest.current) return
      setModels(result.models); update('models', result.models.map((model) => model.id))
      if (result.current_model && !form.model) update('model', result.current_model)
      setMessage(result.models.length ? `已读取 ${result.models.length} 个可用模型。` : '接口未返回可用模型，可继续手工填写模型名称。')
    } catch (reason: unknown) { if (sequence === modelRequest.current) { setModels([]); setError(messageFor(reason, '模型列表读取失败，可继续手工填写模型名称。')) } }
    finally { if (sequence === modelRequest.current) setModelsBusy(false) }
  }
  const save = async () => {
    if (!registry || (!creating && !selected) || (!creating && selected?.config_source === 'environment')) return
    setBusy(true); setError(''); setMessage('')
    const payload: ProviderDraft = { ...form, provider: form.provider.trim(), base_url: form.base_url.trim(), model: form.model.trim(), models: form.models.filter(Boolean), ...(form.api_key?.trim() ? { api_key: form.api_key.trim() } : {}) }
    try {
      const saved = creating ? await api.providers.create(adminToken, { ...payload, ...(copyFrom ? { copy_from: copyFrom } : {}) }) : await api.providers.update(adminToken, selected!.id, payload)
      const next = await api.providers.get(adminToken)
      setRegistry(next); setSelectedId(saved.id); setCreating(false); setCopyFrom(undefined); setModels(saved.models.map((id) => ({ id }))); setForm(formFromConfig(saved));
      setMessage('模型配置已保存。留空 API token 会保留该 profile 的现有 token。')
    } catch (reason: unknown) { setError(messageFor(reason, '配置保存失败，请检查输入后重试。')) }
    finally { setBusy(false) }
  }
  const setDefault = async () => {
    if (!selected) return
    setBusy(true); setError(''); setMessage('')
    try { const result = await api.providers.setDefault(adminToken, selected.id); setRegistry((current) => current ? { ...current, default_provider_id: result.default_provider_id } : current); setMessage('默认 provider 已更新。') }
    catch (reason: unknown) { setError(messageFor(reason, '默认 provider 更新失败。')) }
    finally { setBusy(false) }
  }

  if (!registry) return <div className="page-shell">
    <div className="page-heading"><div><h1>模型配置</h1><p className="heading-copy">管理 OpenAI-compatible Responses provider profile。管理口令和 API token 仅保存在当前页面内存。</p></div></div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    <section className="card editor-card settings-unlock"><h2>管理授权</h2><p className="muted">请输入管理口令以读取 provider 配置。未授权时服务器不会返回任何配置。</p><label className="field field-wide">管理口令<input type="password" value={adminToken} autoComplete="current-password" onChange={(event) => setAdminToken(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void loadRegistry() }} /></label><div className="form-actions"><span className="muted">管理口令不会写入浏览器存储。</span><button className="button button-primary" disabled={busy || !adminToken.trim()} onClick={() => void loadRegistry()}>{busy ? '读取中…' : '解锁配置'}</button></div></section>
  </div>

  const readOnly = Boolean(selected && selected.config_source === 'environment') && !creating
  return <div className="page-shell">
    <div className="page-heading"><div><h1>模型配置</h1><p className="heading-copy">每个 provider 都是独立 profile。环境配置只读；复制环境配置时，API token 仅在你明确保存复制操作时继承。</p></div></div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}{message && <div className="alert alert-warning" role="status">{message}</div>}
    <section className="card editor-card provider-settings-card">
      <div className="card-title-row"><div><span className="step-number">01</span><h2>Provider profiles</h2></div><button type="button" className="button button-secondary" onClick={() => startNew()}>新建自定义 provider</button></div>
      <div className="provider-profile-row"><label className="field field-wide">选择 profile<select aria-label="选择配置 profile" value={creating ? '' : selectedId} onChange={(event) => selectProvider(event.target.value)} disabled={creating || busy}><option value="">{creating ? '新建 profile' : '选择 provider'}</option>{registry.providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.provider || provider.id}{provider.id === registry.default_provider_id ? ' · 默认' : ''}{provider.config_source === 'environment' ? ' · 环境只读' : ''}</option>)}</select></label>{creating && <button type="button" className="button button-secondary" disabled={busy} onClick={() => selectProvider(registry.default_provider_id)}>取消新建</button>}{selected?.config_source === 'environment' && <button type="button" className="button button-secondary" disabled={busy} onClick={() => startNew(selected)}>复制环境配置</button>}{!creating && selected && selected.id !== registry.default_provider_id && <button type="button" className="button button-secondary" disabled={busy || !selected.enabled || !selected.api_key_configured} onClick={() => void setDefault()}>设为默认</button>}</div>
      <div className="settings-grid"><label className="field">启用 provider<input type="checkbox" checked={form.enabled} disabled={readOnly} onChange={(event) => update('enabled', event.target.checked)} /></label><label className="field">提供商名称<input value={form.provider} disabled={readOnly} onChange={(event) => update('provider', event.target.value)} /></label><label className="field field-wide">Base URL<input value={form.base_url} disabled={readOnly} onChange={(event) => update('base_url', event.target.value)} /></label><div className="field field-wide">模型名称<div className="model-picker"><select aria-label="选择接口模型" value={models.some((model) => model.id === form.model) ? form.model : ''} disabled={readOnly || models.length === 0} onChange={(event) => update('model', event.target.value)}><option value="">{models.length ? '选择接口模型' : '先读取模型列表'}</option>{models.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}</select><input aria-label="默认模型 ID" value={form.model} disabled={readOnly} placeholder="也可手工填写模型 ID" onChange={(event) => update('model', event.target.value)} /><button type="button" className="button button-secondary" disabled={modelsBusy} onClick={() => void loadModels()}>{modelsBusy ? '读取中…' : '读取模型'}</button></div></div></div>
      <label className="checkbox-field"><input type="checkbox" checked={form.gateway_messages} disabled={readOnly} onChange={(event) => update('gateway_messages', event.target.checked)} /> 发送可选 messages 网关兼容字段 <span className="muted">（仅需要时开启）</span></label>
      <div className="settings-secret"><h3>API token</h3><p className="muted">当前状态：{selected?.api_key_configured ? '已配置（不会显示或自动填充）' : '未配置'}。留空会保留所选 profile 的 token。</p><label className="field">替换 API token<input type="password" value={form.api_key ?? ''} disabled={readOnly} autoComplete="new-password" placeholder="留空以保留现有 token" onChange={(event) => update('api_key', event.target.value)} /></label></div>
      <div className="form-actions"><span className="muted">{selected?.config_source === 'environment' && !creating ? `环境配置只读${registry.env_path ? `：${registry.env_path}` : ''}。请使用“复制环境配置”创建可保存 profile。` : creating && copyFrom ? '保存将明确从选定环境/profile 复制 token；未填写新 token 时才会继承。' : '当前 profile 的 token 不会被返回或打印。'}</span>{!readOnly && <button className="button button-primary" disabled={busy || !form.provider.trim() || !form.base_url.trim() || !form.model.trim()} onClick={() => void save()}>{busy ? '保存中…' : creating ? '创建 profile' : '保存 profile'}</button>}</div>
    </section>
  </div>
}
