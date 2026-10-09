import { useEffect, useState } from 'react'
import { api } from './api'
import { EvaluationPage } from './pages/EvaluationPage'
import { IngestionPage } from './pages/IngestionPage'
import { WorkbenchPage } from './pages/WorkbenchPage'

type Page = 'workbench' | 'evaluation' | 'ingestion'

function pageFromHash(): Page {
  if (window.location.hash === '#evaluation') return 'evaluation'
  if (window.location.hash === '#ingestion') return 'ingestion'
  return 'workbench'
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash)
  const [apiState, setApiState] = useState<'checking' | 'online' | 'offline'>('checking')

  useEffect(() => {
    const onHashChange = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  useEffect(() => {
    let active = true
    api.health()
      .then(() => { if (active) setApiState('online') })
      .catch(() => { if (active) setApiState('offline') })
    return () => { active = false }
  }, [])

  const navigate = (nextPage: Page) => {
    window.location.hash = nextPage === 'evaluation' ? 'evaluation' : nextPage === 'ingestion' ? 'ingestion' : 'workbench'
  }

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#workbench" aria-label="课程答案评估工作台首页">
          <span className="brand-mark">答</span>
          <span><strong>课程答案评估</strong><small>COURSE ANSWER REVIEW</small></span>
        </a>
        <nav className="main-nav" aria-label="主导航">
          <button className={page === 'workbench' ? 'nav-link active' : 'nav-link'} onClick={() => navigate('workbench')}>工作台</button>
          <button className={page === 'evaluation' ? 'nav-link active' : 'nav-link'} onClick={() => navigate('evaluation')}>评估指标</button>
          <button className={page === 'ingestion' ? 'nav-link active' : 'nav-link'} onClick={() => navigate('ingestion')}>教材导入</button>
        </nav>
        <div className={`api-indicator api-${apiState}`} title={apiState === 'offline' ? '后端不可用，提交时将显示具体错误' : undefined}>
          <span className="api-dot" />{apiState === 'checking' ? '连接检查中' : apiState === 'online' ? 'API 已连接' : 'API 暂不可用'}
        </div>
      </header>
      <main>
        {page === 'workbench' ? <WorkbenchPage onOpenEvaluation={() => navigate('evaluation')} /> : page === 'evaluation' ? <EvaluationPage onOpenWorkbench={() => navigate('workbench')} /> : <IngestionPage />}
      </main>
      <footer className="footer">课程答案评估 · 数据由后端 API 实时提供</footer>
    </div>
  )
}
