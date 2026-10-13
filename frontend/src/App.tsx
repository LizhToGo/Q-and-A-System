import { useEffect, useState } from 'react'
import { api } from './api'
import { EvaluationPage } from './pages/EvaluationPage'
import { IngestionPage } from './pages/IngestionPage'
import { ProviderSettingsPage } from './pages/ProviderSettingsPage'
import { WorkbenchPage } from './pages/WorkbenchPage'

type Page = 'evaluation' | 'ingestion' | 'settings'
type ApiState = 'checking' | 'online' | 'offline'

function pageFromHash(): Page {
  if (window.location.hash === '#ingestion') return 'ingestion'
  if (window.location.hash === '#settings') return 'settings'
  return 'evaluation'
}

function useApiState() {
  const [apiState, setApiState] = useState<ApiState>('checking')
  useEffect(() => {
    let active = true
    api.health()
      .then(() => { if (active) setApiState('online') })
      .catch(() => { if (active) setApiState('offline') })
    return () => { active = false }
  }, [])
  return apiState
}

export default function App() {
  const apiState = useApiState()
  const [page, setPage] = useState<Page>(pageFromHash)
  const isAdmin = window.location.pathname.startsWith('/admin')

  useEffect(() => {
    const onHashChange = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  if (!isAdmin) return <WorkbenchPage apiState={apiState} />

  const navigate = (nextPage: Page) => {
    window.location.hash = nextPage
  }

  return (
    <div className="admin-app">
      <header className="topbar admin-topbar">
        <a className="brand" href="/" aria-label="返回知答问答首页">
          <span className="brand-mark">答</span>
          <span><strong>知答管理后台</strong></span>
        </a>
        <nav className="main-nav" aria-label="管理后台导航">
          <button className={page === 'evaluation' ? 'nav-link active' : 'nav-link'} onClick={() => navigate('evaluation')}>评估指标</button>
          <button className={page === 'ingestion' ? 'nav-link active' : 'nav-link'} onClick={() => navigate('ingestion')}>教材导入</button>
          <button className={page === 'settings' ? 'nav-link active' : 'nav-link'} onClick={() => navigate('settings')}>模型配置</button>
        </nav>
        <div className={`api-indicator api-${apiState}`}><span className="api-dot" />{apiState === 'checking' ? '连接检查中' : apiState === 'online' ? 'API 已连接' : 'API 暂不可用'}</div>
      </header>
      <main className="admin-main">
        {page === 'evaluation' ? <EvaluationPage onOpenWorkbench={() => { window.location.href = '/' }} /> : page === 'ingestion' ? <IngestionPage /> : <ProviderSettingsPage />}
      </main>
      <footer className="footer admin-footer"><a href="/">← 返回问答首页</a><span>知答管理后台 · 数据由后端 API 实时提供</span></footer>
    </div>
  )
}
