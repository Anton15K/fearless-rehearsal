import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { ArrowRight, Crosshair, GitCompareArrows, Lock, Pin as PinIcon, Search, ShieldCheck } from 'lucide-react'
import { api, type NodeState, type Plan } from './api'
import { ActionIcon, Dialog, Portrait } from './common'
import { branchPath, kindLabel, nodeLabel } from './draft'

export function ChampionDialog({ state, select, close, busy }: { state: NodeState; select: (champion: string) => void; close: () => void; busy: boolean }) {
  const [query, setQuery] = useState('')
  const [champion, setChampion] = useState('')
  const verb = state.kind === 'ban' ? 'Ban' : 'Pick'
  const options = state.legal.filter(name => name.toLowerCase().includes(query.trim().toLowerCase())).sort((left, right) => left.localeCompare(right))
  const pick = options.includes(champion) ? champion : options.length === 1 ? options[0] : ''

  return (
    <Dialog title="Choose a legal champion" description={`${state.legal.length} champions available for this ${verb.toLowerCase()}.`} close={close}>
      <form onSubmit={event => { event.preventDefault(); if (pick) select(pick) }}>
        <label className="search-field"><Search size={17} /><input autoFocus type="search" placeholder="Search champions" aria-label="Search champions" value={query} onChange={event => setQuery(event.target.value)} /></label>
        <div className="champion-options" aria-label="Matching champions">
          {options.length > 0 ? options.map(name => (
            <button type="button" key={name} className={`champion-option ${pick === name ? 'champion-selected' : ''}`} aria-pressed={pick === name} onClick={() => setChampion(name)}>
              <Portrait name={name} size={30} /><span>{name}</span>
            </button>
          )) : <p className="muted">No legal champions match your search.</p>}
        </div>
        <div className="dialog-actions"><button className="button button-accent" type="submit" disabled={!pick || busy}><ActionIcon kind={state.kind} /> {verb}{pick && ` ${pick}`} <ArrowRight size={16} /></button></div>
      </form>
    </Dialog>
  )
}

type PinInput = { champion: string; game: number; player: string; backup?: string }

export function PinDialog({ plan, champion, targetGame, backup, champions, state, save, close, busy }: {
  plan: Plan; champion?: string; targetGame?: number; backup?: string; champions: string[]; state: NodeState
  save: (pin: PinInput) => void; close: () => void; busy: boolean
}) {
  const max = Number(plan.format?.slice(-1)) || 5
  const [selected, setSelected] = useState(champion || '')
  const games = Array.from({ length: Math.max(0, max - state.game) }, (_, index) => state.game + index + 1)
  const defaultGame = targetGame ?? plan.target_game ?? state.game + 1
  const sorted = [...champions].sort((left, right) => left.localeCompare(right))

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    save({
      champion: selected,
      game: Number(data.get('game')),
      player: String(data.get('player')).trim(),
      backup: String(data.get('backup') || '') || undefined,
    })
  }

  return (
    <Dialog title="Pin for a future game" description="Track whether this branch preserves the champion for your priority game." close={close}>
      {games.length ? <form className="form-grid" onSubmit={submit}>
        <label className="field">Champion <select required name="champion" aria-label="Champion" value={selected} onChange={event => setSelected(event.target.value)}>
          <option value="">Choose a champion</option>
          {sorted.map(name => <option key={name}>{name}</option>)}
        </select></label>
        <label className="field">Target game <select name="game" aria-label="Target game" defaultValue={games.includes(defaultGame) ? defaultGame : games[0]}>
          {games.map(game => <option key={game} value={game}>Game {game}</option>)}
        </select></label>
        <label className="field">Player or role <input name="player" required defaultValue={plan.player || ''} placeholder="e.g. Mid laner" maxLength={80} /></label>
        <label className="field">Backup <select name="backup" aria-label="Backup" defaultValue={backup || ''}>
          <option value="">No backup chosen</option>
          {sorted.filter(name => name !== selected).map(name => <option key={name}>{name}</option>)}
        </select></label>
        <div className="wide dialog-actions"><button type="submit" className="button button-accent" disabled={busy}><PinIcon size={16} /> Save pin</button></div>
      </form> : <p className="muted">The selected game is the final game of this series.</p>}
    </Dialog>
  )
}

export function CompareDialog({ plan, current, open, close }: { plan: Plan; current: string; open: (id: string) => void; close: () => void }) {
  const options = plan.nodes
  const currentNode = options.find(node => node.id === current)
  const pair = currentNode?.parent_id
    ? options.find(node => node.id !== current && node.parent_id === currentNode.parent_id)
    : undefined
  const children = options.filter(node => node.parent_id === current)
  const alternate = pair || options.filter(node => node.id !== current).at(-1)
  const [left, setLeft] = useState(children.length > 1 ? children[0].id : current)
  const [right, setRight] = useState(children.length > 1 ? children[1].id : alternate?.id || '')
  const [comparison, setComparison] = useState<[NodeState, NodeState] | null>(null)
  const [error, setError] = useState('')
  const labels = useMemo(() => Object.fromEntries(options.map(option => {
    const path = branchPath(options, option.id)
    const sameGame = path.filter(node => node.game === option.game)
    const previous = sameGame.at(-2)
    const action = option.kind === 'ban' || option.kind === 'pick'
    const description = action && option.champion ? `${kindLabel(option.kind)} ${option.champion}` : nodeLabel(option)
    const previousAction = previous?.champion ? ` after ${previous.champion}` : ''
    return [option.id, action ? `Game ${option.game} · ${description}${previousAction}` : description]
  })), [options])

  useEffect(() => {
    const controller = new AbortController()
    setComparison(null)
    setError('')
    if (left && right && left !== right) {
      Promise.all([api.node(plan.id, left, controller.signal), api.node(plan.id, right, controller.signal)])
        .then(setComparison)
        .catch(reason => { if (!controller.signal.aborted) setError(reason.message) })
    }
    return () => controller.abort()
  }, [left, right, plan.id])

  return (
    <Dialog title="Compare branches" description="Compare what each saved position leaves available for the rest of the series." close={close}>
      {options.length < 2 ? <p className="muted compare-empty">Record a draft decision to compare it with the current position. Revisit an earlier node to create an alternate line.</p> :
        <>
          <div className="form-grid compare-selectors">
            {[{ name: 'Branch A', value: left, change: setLeft }, { name: 'Branch B', value: right, change: setRight }].map(branch => (
              <label className="field" key={branch.name}>{branch.name}
                <select aria-label={branch.name} value={branch.value} onChange={event => branch.change(event.target.value)}>
                  {options.map(option => <option key={option.id} value={option.id}>{labels[option.id]}</option>)}
                </select>
              </label>
            ))}
          </div>
          {error && <p className="error-message" role="alert">{error}</p>}
          {left === right ? <p className="muted compare-empty">Select two different positions to compare.</p> :
            comparison ? (
              <div className="compare-body">
                {comparison.map((node, index) => {
                  const other = comparison[1 - index]
                  const unique = node.legal.filter(champ => !other.legal.includes(champ))
                  const used = node.actions.filter(action => action.champion)
                  return (
                    <div className="compare-lane" key={node.node_id}>
                      <span className="section-kicker"><GitCompareArrows size={14} /> Branch {index ? 'B' : 'A'}</span>
                      <h3>{labels[node.node_id]}</h3>
                      <div className="compare-stats"><span><ShieldCheck size={14} /> {node.legal.length} legal</span><span><Lock size={14} /> {node.locked.length} locked</span></div>
                      <div className="compare-champs" aria-label="Champions used in this draft">{used.map((action, i) => (
                        <span key={i} title={`${action.team} ${action.kind} · ${action.champion}`}><Portrait name={action.champion!} size={25} muted={action.kind === 'ban'} /><span className="sr-only">{action.champion}</span></span>
                      ))}</div>
                      <p>{unique.length ? `Only legal here: ${unique.join(', ')}.` : 'No champions uniquely legal here.'}</p>
                      {node.pins.length > 0 && <ul className="compare-pins">{node.pins.map(pin => <li key={`${pin.game}-${pin.champion}-${pin.player}`}>G{pin.game} {pin.champion} · {pin.status.replaceAll('_', ' ')}</li>)}</ul>}
                      <button type="button" className="button button-secondary button-small" onClick={() => open(node.node_id)}>Open branch <ArrowRight size={15} /></button>
                    </div>
                  )
                })}
              </div>
            ) : <p className="muted compare-empty" role="status">Loading branch positions…</p>}
        </>}
    </Dialog>
  )
}

export function briefTemplate(plan: Plan, state: NodeState) {
  const current = plan.nodes.find(node => node.id === state.node_id)
  const decisions = state.actions.filter(action => action.champion).map(action =>
    `${action.team} ${action.kind === 'ban' ? 'bans' : 'picks'} ${action.champion}`,
  )
  const lines = [`${plan.title} · Game ${state.game} · Patch ${state.patch ?? plan.patch}`]
  if (plan.priority) lines.push(`Priority: ${plan.priority}`)
  if (current) lines.push(`Position: ${nodeLabel(current)}`)
  if (decisions.length) lines.push(`Draft: ${decisions.join(', ')}.`)
  lines.push(state.draft_mode === 'standard' ? 'Standard tournament draft.' : `${state.locked.length} champions locked by Fearless Draft.`)
  for (const pin of state.pins) {
    lines.push(`Game ${pin.game} · ${pin.player}: ${pin.champion} (${pin.status.replaceAll('_', ' ')}).${pin.backup ? ` Backup: ${pin.backup}.` : ''}`)
  }
  return lines.join('\n')
}

export function CoachBrief({ plan, state, save, busy }: {
  plan: Plan; state: NodeState; save: (text: string, backup?: string) => void; busy: boolean
}) {
  const saved = plan.brief.find(note => note.node_id === state.node_id)
  const [text, setText] = useState(saved?.text || briefTemplate(plan, state))
  const [backup, setBackup] = useState(saved?.backup || '')
  const [copyStatus, setCopyStatus] = useState('')
  const node = plan.nodes.find(candidate => candidate.id === state.node_id)
  const available = [...state.legal].sort((left, right) => left.localeCompare(right))
  if (backup && !available.includes(backup)) available.unshift(backup)

  async function copy() {
    try {
      await navigator.clipboard.writeText(backup ? `${text}\nBackup option: ${backup}` : text)
      setCopyStatus('Brief copied')
    } catch {
      setCopyStatus('Select the brief text to copy it.')
    }
  }

  return (
    <div className="prep-grid brief-grid">
      <section className="panel prep-panel brief-form" aria-labelledby="brief-form-title">
        <header className="panel-heading"><div><span className="section-kicker"><Crosshair size={15} /> Game {state.game} handoff</span><h2 id="brief-form-title">Brief for this branch</h2></div></header>
        <form className="form-grid" onSubmit={event => { event.preventDefault(); if (text.trim()) save(text.trim(), backup || undefined) }}>
          <label className="field wide">Coach notes <textarea rows={11} value={text} onChange={event => setText(event.target.value)} required /></label>
          <label className="field wide">Backup champion <select aria-label="Backup champion" value={backup} onChange={event => setBackup(event.target.value)}>
            <option value="">No backup chosen</option>
            {available.map(champ => <option key={champ}>{champ}</option>)}
          </select></label>
          <div className="wide form-actions"><span className="muted" role="status">{copyStatus || (saved ? 'Saved to this branch' : '')}</span><button type="button" className="button button-secondary" onClick={copy}>Copy brief</button><button type="submit" disabled={busy} className="button button-accent">Save brief <ArrowRight size={15} /></button></div>
        </form>
      </section>
      <section className="panel prep-panel brief-context" aria-labelledby="brief-context-title">
        <header className="panel-heading"><div><span className="section-kicker"><Lock size={15} /> Position</span><h2 id="brief-context-title">Draft context</h2></div></header>
        <dl>
          <div><dt>Selected node</dt><dd>{node ? nodeLabel(node) : `Game ${state.game}`}</dd></div>
          <div><dt>First pick</dt><dd>{state.first}</dd></div>
          <div><dt>Series priority</dt><dd>{plan.priority || 'No priority set'}</dd></div>
          {state.draft_mode === 'standard' ? <div><dt>Draft format</dt><dd>Standard</dd></div> : <div><dt>Fearless lockout</dt><dd>{state.locked.length} champions</dd></div>}
          <div><dt>Future-game pins</dt><dd>{state.pins.length}</dd></div>
        </dl>
      </section>
    </div>
  )
}
