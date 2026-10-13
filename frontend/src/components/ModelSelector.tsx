import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { api, type PublicProvider } from '../api'

export type ModelSelection = { providerId: string; model: string }

type ModelCatalog = {
  signature: string
  models: string[]
  status: 'loading' | 'ready' | 'failed'
}

interface ModelSelectorProps {
  providers: PublicProvider[]
  selection: ModelSelection
  loading: boolean
  onSelect: (selection: ModelSelection) => void
  onDiscoveryError: (message: string) => void
}

function providerLabel(provider: PublicProvider) {
  return `${provider.provider} · ${provider.id === 'environment' ? '环境配置' : '保存配置'} · ${provider.id}`
}

function providerSignature(provider: PublicProvider) {
  return JSON.stringify([provider.model, provider.models])
}

export function ModelSelector({ providers, selection, loading, onSelect, onDiscoveryError }: ModelSelectorProps) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [catalogs, setCatalogs] = useState<Record<string, ModelCatalog>>({})
  const [customOpen, setCustomOpen] = useState(false)
  const [customProviderId, setCustomProviderId] = useState('')
  const [customModel, setCustomModel] = useState('')
  const cache = useRef<Record<string, ModelCatalog>>({})
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const customInputRef = useRef<HTMLInputElement>(null)
  const dialogId = useId()
  const customId = useId()
  const provider = providers.find((item) => item.id === selection.providerId)
  const currentLabel = provider ? `${providerLabel(provider)} / ${selection.model || '未选择模型 ID'}` : '尚未配置可用模型'

  const close = (restoreFocus = false) => {
    setOpen(false)
    setCustomOpen(false)
    if (restoreFocus) triggerRef.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    searchRef.current?.focus()
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) {
        setOpen(false)
        setCustomOpen(false)
      }
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setOpen(false)
      setCustomOpen(false)
      triggerRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    let active = true
    // Discover only while the menu is in use; closing or refreshing profiles invalidates pending responses.
    for (const item of providers) {
      const signature = providerSignature(item)
      const saved = cache.current[item.id]
      if (saved?.signature === signature && saved.status === 'ready') continue
      setCatalogs((current) => ({ ...current, [item.id]: { signature, models: [], status: 'loading' } }))
      api.providerModels(item.id)
        .then((data) => {
          if (!active) return
          const catalog: ModelCatalog = { signature, models: data.models.map((model) => model.id), status: 'ready' }
          cache.current[item.id] = catalog
          setCatalogs((current) => ({ ...current, [item.id]: catalog }))
        })
        .catch(() => {
          if (!active) return
          setCatalogs((current) => ({ ...current, [item.id]: { signature, models: [], status: 'failed' } }))
        })
    }
    return () => { active = false }
  }, [open, providers])

  useEffect(() => {
    const failed = providers.some((item) => catalogs[item.id]?.signature === providerSignature(item) && catalogs[item.id]?.status === 'failed')
    onDiscoveryError(failed ? '部分模型列表发现失败。仍可选择配置中的默认／保存模型，或在顶部菜单使用“自定义模型 ID”。' : '')
  }, [catalogs, providers, onDiscoveryError])

  useEffect(() => {
    if (customOpen) customInputRef.current?.focus()
  }, [customOpen])

  const choose = (providerId: string, model: string) => {
    onSelect({ providerId, model })
    close(true)
  }

  const navigateModels = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const target = event.target
    if (!(target instanceof HTMLElement) || (target !== searchRef.current && !target.matches('[data-model-option]'))) return
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    if (target === searchRef.current && !['ArrowDown', 'ArrowUp'].includes(event.key)) return
    const options = Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>('[data-model-option]') ?? [])
    if (!options.length) return
    event.preventDefault()
    const index = options.indexOf(target as HTMLButtonElement)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : index < 0 ? (event.key === 'ArrowUp' ? options.length - 1 : 0) : (index + (event.key === 'ArrowUp' ? -1 : 1) + options.length) % options.length
    options[next].focus()
  }

  const query = search.trim().toLowerCase()
  const groups = providers.map((item) => {
    const entry = catalogs[item.id]
    const catalog = entry?.signature === providerSignature(item) ? entry : undefined
    const models = [...new Set([item.model, ...item.models, ...(catalog?.models ?? []), ...(selection.providerId === item.id ? [selection.model] : [])].filter(Boolean))]
    return { provider: item, catalog, models: models.filter((model) => `${providerLabel(item)} ${model}`.toLowerCase().includes(query)) }
  })
  const hasMatches = groups.some((group) => group.models.length > 0)
  const customProvider = providers.find((item) => item.id === customProviderId)

  return <div className="model-selector" ref={rootRef} onBlur={(event) => {
    if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) close()
  }}>
    <button type="button" className="model-selector-trigger" ref={triggerRef} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? dialogId : undefined} title={currentLabel} onClick={() => {
      if (open) { close(); return }
      setSearch('')
      setCustomOpen(false)
      setCustomProviderId(selection.providerId || providers[0]?.id || '')
      setCustomModel('')
      setOpen(true)
    }}>
      <span className="model-trigger-name">{provider ? selection.model.split('/').filter(Boolean).pop() || '选择模型' : loading ? '模型配置加载中…' : '选择模型'}</span>
      <span className="model-trigger-chevron" aria-hidden="true">⌄</span>
    </button>
    {open && <div className="model-selector-popover" id={dialogId} role="dialog" aria-label="选择回答模型" onKeyDown={navigateModels}>
      <label className="model-search-label">搜索模型或提供商
        <input ref={searchRef} type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称或完整模型 ID" />
      </label>
      <p className="model-current-selection">当前：{currentLabel}</p>
      <div className="model-selector-groups" aria-label="按提供商分组的模型">
        {groups.filter((group) => !query || group.models.length > 0).map((group) => <section className="model-provider-group" key={group.provider.id} aria-label={providerLabel(group.provider)}>
          <h3>{group.provider.provider}<small>{group.provider.id === 'environment' ? '环境配置' : '保存配置'} · {group.provider.id}</small></h3>
          {group.catalog?.status === 'loading' && <p className="model-discovery-status" role="status">正在发现模型，配置模型可直接选择…</p>}
          {group.catalog?.status === 'failed' && <p className="model-discovery-status model-discovery-failed">模型发现失败；保留配置模型，也可使用自定义 ID。</p>}
          {group.models.map((model) => {
            const selected = selection.providerId === group.provider.id && selection.model === model
            return <button type="button" data-model-option className={`model-option${selected ? ' is-selected' : ''}`} key={model} aria-pressed={selected} title={model} onClick={() => choose(group.provider.id, model)}>
              <span className="model-option-copy"><span className="model-option-name">{model.split('/').filter(Boolean).pop() || model}</span><small>完整 ID：{model}</small></span>
              <span className="model-option-check">{selected ? '已选' : ''}</span>
            </button>
          })}
          {!group.models.length && !query && <p className="model-discovery-status">尚无可选模型 ID，请使用下方自定义入口。</p>}
        </section>)}
        {!hasMatches && <p className="model-empty">{providers.length ? query ? '没有匹配的模型；可修改搜索或输入自定义 ID。' : '暂无模型 ID。可使用下方自定义入口。' : loading ? '正在加载提供商配置…' : '暂无可用提供商，请先到模型配置中启用。'}</p>}
      </div>
      <div className="model-custom-section">
        <button type="button" className="model-custom-toggle" aria-expanded={customOpen} aria-controls={customId} disabled={!providers.length} onClick={() => setCustomOpen((current) => !current)}>自定义模型 ID <span aria-hidden="true">{customOpen ? '−' : '＋'}</span></button>
        {customOpen && <div className="model-custom-fields" id={customId}>
          <label>已配置提供商<select aria-label="自定义模型的提供商" value={customProvider ? customProviderId : ''} onChange={(event) => setCustomProviderId(event.target.value)}><option value="" disabled>选择已配置提供商</option>{providers.map((item) => <option key={item.id} value={item.id}>{providerLabel(item)}</option>)}</select></label>
          <label>完整模型 ID<input ref={customInputRef} value={customModel} onChange={(event) => setCustomModel(event.target.value)} placeholder="输入服务商提供的完整模型 ID" autoComplete="off" onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              if (customProvider && customModel.trim()) choose(customProvider.id, customModel.trim())
            }
          }} /></label>
          <p>不会添加或更改提供商配置；此 ID 仅用于当前对话请求。</p>
          <button type="button" className="model-custom-apply" disabled={!customProvider || !customModel.trim()} onClick={() => { if (customProvider) choose(customProvider.id, customModel.trim()) }}>应用模型</button>
        </div>}
      </div>
    </div>}
  </div>
}
