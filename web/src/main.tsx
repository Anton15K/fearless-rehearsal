import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  Activity, AlertCircle, ArrowRight, CircleDot, ClipboardList, GitBranch, LoaderCircle,
  NotebookPen, Radio, Swords, X,
} from 'lucide-react'
import '@fontsource/barlow-condensed/latin-600.css'
import '@fontsource/barlow-condensed/latin-700.css'
import '@fontsource-variable/ibm-plex-sans'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import { api, type Bootstrap, type NodeState, type Plan, type PlanInput, type PlanSummary } from './api'
import { Board, RulesContent } from './Board'
import { Dialog } from './common'
import { ChampionDialog, CoachBrief, CompareDialog, PinDialog } from './Dialogs'
import { MatchPrep, matchFormat } from './Prep'
import './style.css'

type View = 'prep' | 'board' | 'brief'
type Modal =
  | { type: 'compare' | 'rules' }
  | { type: 'champion'; first?: string }
  | { type: 'pin'; champion?: string; game?: number; backup?: string }

const tabs = [
  { id: 'prep', name: 'Match prep', icon: ClipboardList },
  { id: 'board', name: 'Rehearsal board', icon: GitBranch },
  { id: 'brief', name: 'Coach brief', icon: NotebookPen },
] as const
const LAST_PLAN = 'fearless.last-plan'
const lastNodeKey = (planId: string) => `fearless.node.${planId}`

function readStored(key: string) {
  try { return localStorage.getItem(key) } catch { return null }
}

function writeStored(key: string, value: string) {
  try { localStorage.setItem(key, value) } catch { /* A blocked storage context can still use the board. */ }
}

function App() {
  const [view, setView] = useState<View>('board')
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [plans, setPlans] = useState<PlanSummary[]>([])
  const [plan, setPlan] = useState<Plan | null>(null)
  const [selected, setSelected] = useState('')
  const [state, setState] = useState<NodeState | null>(null)
  const [modal, setModal] = useState<Modal | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [nodeError, setNodeError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [startupRetry, setStartupRetry] = useState(0)

  function applyPlan(updated: Plan, nodeId: string) {
    setPlan(updated)
    setSelected(nodeId)
    writeStored(LAST_PLAN, updated.id)
    writeStored(lastNodeKey(updated.id), nodeId)
    setPlans(current => [updated, ...current.filter(candidate => candidate.id !== updated.id)])
  }

  function restore(updated: Plan) {
    const previous = readStored(lastNodeKey(updated.id))
    applyPlan(updated, previous && updated.nodes.some(node => node.id === previous) ? previous : updated.selected_node || updated.root_id)
  }

  useEffect(() => {
    let active = true
    async function prepare() {
      setLoading(true)
      setError('')
      try {
        const [config, saved] = await Promise.all([api.bootstrap(), api.plans()])
        if (!active) return
        setBootstrap(config)
        setPlans(saved)
        const lastId = readStored(LAST_PLAN)
        const recent = saved.find(candidate => candidate.id === lastId) || saved[0]
        const initial = recent ? await api.plan(recent.id) : await api.create({ match_id: config.default_match })
        if (active) restore(initial)
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : 'The rehearsal service is unavailable.')
      } finally {
        if (active) setLoading(false)
      }
    }
    void prepare()
    return () => { active = false }
  }, [startupRetry])

  const planId = plan?.id
  useEffect(() => {
    if (!planId || !selected) return
    const controller = new AbortController()
    setNodeError('')
    api.node(planId, selected, controller.signal)
      .then(setState)
      .catch(reason => { if (!controller.signal.aborted) setNodeError(reason.message) })
    return () => controller.abort()
  }, [planId, selected, refresh])

  async function mutate(action: () => Promise<Plan>, success: (updated: Plan) => void) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      success(await action())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The change could not be saved.')
    } finally {
      setBusy(false)
    }
  }

  function chooseNode(id: string) {
    if (!plan || id === selected) return
    setState(null)
    setSelected(id)
    writeStored(lastNodeKey(plan.id), id)
  }

  function openPlan(id: string) {
    return mutate(() => api.plan(id), updated => {
      if (id !== plan?.id) setState(null)
      restore(updated)
      setView('board')
    })
  }

  function createPlan(input: PlanInput) {
    return mutate(() => api.create(input), updated => {
      setState(null)
      applyPlan(updated, updated.selected_node || updated.root_id)
      setView('board')
    })
  }

  function changeDraft(result: Plan) {
    if (result.selected_node !== selected) setState(null)
    applyPlan(result, result.selected_node)
    setModal(null)
  }

  function commit(champion: string | null, first?: string) {
    if (!plan || !state) return
    void mutate(() => api.act(plan.id, state.node_id, champion, first), changeDraft)
  }

  function openPin(champion?: string, game?: number, backup?: string) {
    setModal({ type: 'pin', champion, game, backup })
  }

  const match = bootstrap?.matches.find(candidate => candidate.id === plan?.match_id)
  const scenario = match?.source || plan?.source
  const historical = scenario === 'historical'
  const prospective = scenario === 'prospective'
  const teamsWithoutHistory = Object.entries(plan?.team_history ?? {})
    .filter(([, evidence]) => evidence === null).map(([team]) => team)
  const ready = Boolean(bootstrap && plan)
  const tab = tabs.find(item => item.id === view)!
  const context = [plan && `Patch ${plan.patch}`, plan && matchFormat(match ?? plan), plan?.date && (prospective ? `As of ${plan.date}` : plan.date)].filter(Boolean)
  const current = state?.node_id === selected ? state : null

  return (
    <>
      <a className="skip-link" href="#workspace">Skip to workspace</a>
      <header className="global-header">
        <div className="global-inner">
          <button className="brand" type="button" onClick={() => setView('board')} aria-label="Fearless Rehearsal home">
            <span className="brand-mark"><Swords size={20} strokeWidth={2.2} /></span>
            <span className="brand-copy"><strong>FEARLESS</strong><small>REHEARSAL</small></span>
          </button>
          <nav aria-label="Workspace">
            {tabs.map(item => <button key={item.id} type="button" className={`nav-button ${view === item.id ? 'nav-active' : ''}`}
              aria-current={view === item.id ? 'page' : undefined} onClick={() => setView(item.id)}><item.icon size={16} /> {item.name}</button>)}
          </nav>
          <span className="header-mode"><span className="status-dot" /> DRAFT WORKSPACE</span>
        </div>
      </header>
      <main className="app-shell" id="workspace">
        <div className="workspace-heading">
          <div className="workspace-heading-copy">
            <div className="breadcrumb"><span>Workspace</span><span>/</span><span>{tab.name}</span></div>
            <h1>{tab.name}</h1>
            {view === 'board' && plan?.priority && <p className="priority-line"><CircleDot size={14} />{plan.priority}</p>}
          </div>
          {plan && <div className="match-context">
            {(historical || scenario === 'illustrative') && <span className={`source-label ${historical ? 'source-historical' : ''}`}><Radio size={12} /> {historical ? (current?.history ? 'Historical replay' : 'Historical match · Rehearsal') : 'Illustrative scenario'}</span>}
            {prospective && plan.coach_team && <span className="prospective-coach">Your team · {plan.coach_team}</span>}
            <strong>{match?.title || plan.title}</strong>
            <div className="context-meta">{context.map(item => <span key={item}>{item}</span>)}</div>
            {prospective && teamsWithoutHistory.length > 0 && <p className="prospective-coverage">Limited team evidence for {teamsWithoutHistory.join(' and ')}</p>}
          </div>}
        </div>
        {error && <div className="error-banner" role="alert"><AlertCircle size={19} /><span>{error}</span>{ready && <button type="button" className="icon-button" onClick={() => setError('')} aria-label="Dismiss error"><X size={16} /></button>}</div>}
        {loading ? <div className="panel empty" role="status"><LoaderCircle className="spin" size={21} /> Loading rehearsal…</div> :
          !bootstrap || !plan ? (
            <div className="panel empty"><Activity size={25} /><h2>Rehearsal is unavailable</h2><button type="button" className="button button-secondary" onClick={() => setStartupRetry(value => value + 1)}>Try again <ArrowRight size={16} /></button></div>
          ) : (
            <>
              {view === 'prep' && <MatchPrep key={plan.id} bootstrap={bootstrap} plan={plan} plans={plans} busy={busy} create={createPlan} open={openPlan} />}
              {view !== 'prep' && (!current || nodeError) && <div className="panel empty" role={nodeError ? 'alert' : 'status'}>
                {nodeError ? <><AlertCircle size={20} /> {nodeError} <button type="button" className="button button-secondary" onClick={() => setRefresh(value => value + 1)}>Reload position</button></> :
                  <><LoaderCircle className="spin" size={21} /> Loading draft position…</>}
              </div>}
              {view === 'board' && current && !nodeError && <Board key={`${plan.id}-${current.node_id}`} plan={plan} state={current} busy={busy} chooseNode={chooseNode} commit={commit}
                advance={() => void mutate(() => api.advance(plan.id, current.node_id), changeDraft)}
                openCustom={first => setModal({ type: 'champion', first })}
                openPin={openPin} openCompare={() => setModal({ type: 'compare' })} openRules={() => setModal({ type: 'rules' })} />}
              {view === 'brief' && current && !nodeError && <CoachBrief key={`${plan.id}-${current.node_id}`} plan={plan} state={current} busy={busy}
                save={(text, backup) => void mutate(() => api.brief(plan.id, { node_id: current.node_id, text, backup }), updated => applyPlan(updated, current.node_id))} />}
              {modal && current && (
                <>
                  {modal.type === 'champion' && <ChampionDialog state={current} busy={busy} close={() => setModal(null)} select={champion => commit(champion, modal.first)} />}
                  {modal.type === 'pin' && <PinDialog plan={plan} state={current} champions={bootstrap.champions} champion={modal.champion}
                    targetGame={modal.game} backup={modal.backup} busy={busy} close={() => setModal(null)}
                    save={pin => void mutate(() => api.pin(plan.id, pin), updated => {
                      applyPlan(updated, current.node_id)
                      setRefresh(value => value + 1)
                      setModal(null)
                    })} />}
                  {modal.type === 'compare' && <CompareDialog plan={plan} current={current.node_id} close={() => setModal(null)}
                    open={id => { setModal(null); chooseNode(id) }} />}
                  {modal.type === 'rules' && <Dialog title="Fearless Draft rules" close={() => setModal(null)}><RulesContent state={current} /></Dialog>}
                </>
              )}
            </>
          )}
        <footer className="footer"><span>FEARLESS REHEARSAL</span><a href="https://developer.riotgames.com/docs/lol#data-dragon" target="_blank" rel="noreferrer">Champion portraits from Riot Games</a></footer>
      </main>
    </>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
