import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDown, ArrowLeftRight, ArrowRight, ArrowUpRight, Ban, ChevronRight, CircleDot, CircleHelp,
  Crosshair, GitBranch, GitCompareArrows, Link2, ListFilter, Lock, Pin as PinIcon,
  Play, Plus, ShieldCheck, Sparkles, Workflow,
} from 'lucide-react'
import type { NodeState, Pin, Plan } from './api'
import { ActionIcon, LockCount, Portrait } from './common'
import { branchPath, kindLabel, nodeLabel, phaseLabel, slotGroups, visibleBranches } from './draft'
import { matchFormat } from './Prep'
import { OutlookPanel } from './OutlookPanel'
import { isEventEstimate, isPublicEstimate, probabilityLabel } from './outlook'

type BoardProps = {
  plan: Plan
  state: NodeState
  busy: boolean
  chooseNode: (id: string) => void
  commit: (champion: string | null, first?: string) => void
  advance: () => void
  openCustom: (first?: string) => void
  openPin: (champion?: string, game?: number, backup?: string) => void
  openCompare: () => void
  openRules: () => void
}

export function Board({ plan, state, busy, chooseNode, commit, advance, openCustom, openPin, openCompare, openRules }: BoardProps) {
  const [first, setFirst] = useState(state.first)
  const atOpening = state.actions.length === 0 && !state.completed
  const displayFirst = atOpening ? first : state.first
  const displaySecond = plan.teams.find(team => team !== displayFirst) || state.second
  const activeTeam = atOpening ? displayFirst : state.team
  const betweenGames = state.kind === 'next_game'
  const move = (champion: string | null) => commit(champion, atOpening ? displayFirst : undefined)

  return (
    <div className="board-stack">
      <GameStrip plan={plan} state={state} select={chooseNode} future={game => openPin(undefined, game)} />
      <section className="panel draft-panel" aria-labelledby="draft-title">
        <div className="draft-heading">
          <div className="draft-title-group">
            <span className="game-tile">G{state.game}</span>
            <div>
              <h2 id="draft-title">Draft sequence</h2>
              <span className="draft-subtitle">{phaseLabel(state.kind, state.actions)}</span>
            </div>
          </div>
          <div className="draft-tools">
            {atOpening && <label className="first-control">
              <ArrowLeftRight size={14} />
              <span>First pick</span>
              <select value={displayFirst} onChange={event => setFirst(event.target.value)} disabled={busy} aria-label="First pick side">
                {plan.teams.map(team => <option key={team}>{team}</option>)}
              </select>
            </label>}
            <button type="button" className="button button-small button-quiet" onClick={openRules}><CircleHelp size={15} /> Fearless rules</button>
          </div>
        </div>
        <DraftMatrix state={state} first={displayFirst} second={displaySecond} team={activeTeam} />
        {plan.disabled && plan.disabled.length > 0 && (
          <div className="lock-strip disabled-strip">
            <div className="lock-strip-label"><Ban size={14} /><strong>Disabled for this series</strong></div>
            <span className="lock-icons">{plan.disabled.join(', ')}</span>
          </div>
        )}
        <div className="lock-strip">
          <div className="lock-strip-label"><Lock size={14} /><strong>Fearless lockout</strong><span className="mono">{state.locked.length}</span></div>
          <div className="lock-icons" aria-label="Champions picked in earlier games">
            {state.locked.length ? state.locked.slice(0, 14).map(champion => (
              <span key={champion} title={champion}><Portrait name={champion} size={24} muted /><span className="sr-only">{champion}</span></span>
            )) : <span className="muted">No champions carried over</span>}
          </div>
          {state.locked.length > 14 &&
            <button type="button" className="text-button" onClick={openRules}>+{state.locked.length - 14} more <ChevronRight size={13} /></button>}
        </div>
      </section>
      <div className="board-columns">
        <div className="board-main">
          <BranchMap plan={plan} state={state} select={chooseNode} compare={openCompare} custom={() => openCustom(atOpening ? displayFirst : undefined)} busy={busy} />
          <PinPanel pins={state.pins} add={() => openPin()} maxGame={Number(matchFormat(plan).slice(-1)) || 5} game={state.game} />
        </div>
        <div className="board-inspector">
          {betweenGames ? <NextGame state={state} teams={plan.teams} busy={busy} commit={commit} advance={advance} /> :
            <DecisionPanel
              key={state.node_id}
              state={state}
              team={activeTeam}
              busy={busy}
              commit={move}
              advance={advance}
              historical={displayFirst === state.first}
              custom={() => openCustom(atOpening ? displayFirst : undefined)}
              pin={state.game < (Number(matchFormat(plan).slice(-1)) || 5) ? champion => openPin(champion) : undefined}
            />}
          <OutlookPanel outlook={state.outlook} unavailable={plan.source === 'historical' && Boolean(activeTeam && plan.coach_team && activeTeam !== plan.coach_team)} />
        </div>
      </div>
    </div>
  )
}

function NextGame({ state, teams, busy, commit, advance }: {
  state: NodeState; teams: string[]; busy: boolean; commit: BoardProps['commit']; advance: () => void
}) {
  const [first, setFirst] = useState(state.first)
  const game = state.game + 1
  return (
    <section className="panel decision-panel" aria-labelledby="next-game-title">
      <header className="panel-heading decision-header">
        <div><span className="section-kicker"><Crosshair size={15} /> Game {state.game} draft complete</span><h2 id="next-game-title">Start Game {game}</h2></div>
      </header>
      {state.history && <button type="button" className="historical-step" onClick={advance} disabled={busy}>
        <Play size={15} /><span>Start historical Game {game}</span><ChevronRight size={16} />
      </button>}
      <form className="next-game-form" onSubmit={event => { event.preventDefault(); commit(null, first) }}>
        <label className="field">Game {game} first pick <select aria-label={`Game ${game} first pick`} value={first} onChange={event => setFirst(event.target.value)}>
          {teams.map(team => <option key={team}>{team}</option>)}
        </select></label>
        <button className="button button-accent" type="submit" disabled={busy}>Start Game {game} rehearsal <ArrowRight size={15} /></button>
      </form>
    </section>
  )
}

function GameStrip({ plan, state, select, future }: { plan: Plan; state: NodeState; select: (id: string) => void; future: (game: number) => void }) {
  const path = branchPath(plan.nodes, state.node_id)
  const format = Number(matchFormat(plan).slice(-1)) || 5
  return (
    <section className="game-strip" aria-label="Series games">
      {Array.from({ length: format }, (_, index) => {
        const game = index + 1
        const node = path.filter(candidate => candidate.game === game).at(-1)
        const current = game === state.game
        const past = game < state.game
        const pinCount = plan.pins.filter(pin => pin.game === game).length
        return (
          <button key={game} className={`game-tab ${current ? 'game-active' : ''} ${past ? 'game-past' : ''}`}
            type="button" aria-current={current ? 'step' : undefined} disabled={past && !node}
            title={past && !node ? 'Earlier game contributes the existing Fearless lockout.' : undefined}
            onClick={() => node ? select(node.id) : future(game)}>
            <span className="game-tab-main"><span className="game-index">{String(game).padStart(2, '0')}</span><strong>Game {game}</strong></span>
            <span className="game-tab-meta">{current ? <><span className="status-dot" /> Active draft</> : past ? <><Lock size={11} /> Prior draft</> : pinCount ? <><PinIcon size={11} /> {pinCount} pinned</> : 'Upcoming'}</span>
          </button>
        )
      })}
    </section>
  )
}

const groups = [
  { name: 'Bans I', kind: 'ban' }, { name: 'Picks I', kind: 'pick' },
  { name: 'Bans II', kind: 'ban' }, { name: 'Picks II', kind: 'pick' },
]

function DraftMatrix({ state, first, second, team }: { state: NodeState; first: string; second: string; team: string | null }) {
  const nextGroup = state.actions.length < 6 ? 0 : state.actions.length < 12 ? 1 : state.actions.length < 16 ? 2 : 3
  return (
    <div className="draft-scroll" tabIndex={0} role="region" aria-label="Draft picks and bans">
      <div className="draft-matrix">
        <div className="matrix-labels">
          <span>Side</span>
          {groups.map((group, index) => (
            <span className={`matrix-label ${index === nextGroup && !state.completed && state.kind !== 'next_game' ? 'phase-active' : ''}`} key={group.name}>
              <ActionIcon kind={group.kind} size={12} /> {group.name}
            </span>
          ))}
        </div>
        {[first, second].map((side, sideIndex) => (
          <div className={`matrix-row ${sideIndex ? 'side-red' : 'side-blue'}`} key={side}>
            <div className="team-label"><span className="side-dot" /><div><strong>{side}</strong><small>{sideIndex ? 'SECOND PICK' : 'FIRST PICK'}</small></div></div>
            {slotGroups(state.actions, side).map((group, groupIndex) => (
              <div className="slot-group" key={groups[groupIndex].name}>
                {group.map((action, slotIndex) => {
                  const pending = !state.completed && side === team && groupIndex === nextGroup && !action && group.findIndex(slot => !slot) === slotIndex
                  return (
                    <div className={`draft-slot ${action ? 'slot-filled' : ''} ${pending ? 'slot-next' : ''}`} key={slotIndex}
                      title={action?.champion || (pending ? `Next ${state.kind} for ${side}` : undefined)}>
                      {action?.champion ? <><Portrait name={action.champion} size={31} muted={action.kind === 'ban'} /><span>{action.champion}</span></> :
                        action ? <span>Pass</span> : pending ? <><CircleDot size={16} /><span>Next</span></> : <span className="slot-dash">–</span>}
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

function BranchMap({ plan, state, select, compare, custom, busy }: {
  plan: Plan; state: NodeState; select: (id: string) => void; compare: () => void; custom: () => void; busy: boolean
}) {
  const path = useMemo(() => branchPath(plan.nodes, state.node_id), [plan.nodes, state.node_id])
  const onPath = new Set(path.map(node => node.id))
  const layers = visibleBranches(plan.nodes, state.node_id)
  const nextGames = state.kind === 'next_game' ? plan.nodes.filter(node => node.parent_id === state.node_id && node.game > state.game) : []
  if (nextGames.length) layers.push(nextGames)
  const track = useRef<HTMLDivElement>(null)
  const compareReady = plan.nodes.some(node =>
    node.parent_id !== null && plan.nodes.some(sibling => sibling.id !== node.id && sibling.parent_id === node.parent_id),
  )

  useEffect(() => {
    const container = track.current
    const selected = container?.querySelector<HTMLElement>('.branch-selected')
    if (selected && container) {
      const offset = selected.getBoundingClientRect().left - container.getBoundingClientRect().left
      if (offset < 0 || offset + selected.clientWidth > container.clientWidth) {
        container.scrollLeft += offset - Math.max(18, (container.clientWidth - selected.clientWidth) / 2)
      }
    }
  }, [state.node_id])

  return (
    <section className="panel branch-panel" aria-labelledby="branch-title">
      <header className="panel-heading">
        <div><span className="section-kicker"><GitBranch size={15} /> Draft tree</span><h2 id="branch-title">Branch map</h2></div>
        <button type="button" className="button button-small button-secondary" onClick={compare} aria-label="Compare branches">
          <GitCompareArrows size={16} /> Compare
        </button>
      </header>
      <div className="branch-canvas">
        <div className="branch-canvas-heading"><span><span className="status-dot" /> Selected path</span><span><GitBranch size={12} /> {plan.nodes.length} {plan.nodes.length === 1 ? 'node' : 'nodes'}</span></div>
        <div className="tree-track" ref={track} aria-label="Saved draft branches">
          {layers.map((layer, layerIndex) => (
            <div className="tree-tier" key={layer[0].id}>
              {layerIndex > 0 && <span className="tier-line" />}
              {layer.map(node => {
                const active = node.id === state.node_id
                const pathNode = onPath.has(node.id)
                return (
                  <button type="button" key={node.id} className={`branch-node ${active ? 'branch-selected' : ''} ${pathNode ? 'branch-path' : ''}`}
                    aria-pressed={active} onClick={() => select(node.id)} disabled={busy}>
                    <span className="branch-node-type">{node.kind === 'ban' || node.kind === 'pick' ? <ActionIcon kind={node.kind} size={12} /> : <GitBranch size={12} />}
                      {node.kind === 'ban' || node.kind === 'pick' ? `${node.team} ${kindLabel(node.kind).toLowerCase()}` : `Game ${node.game}`}
                    </span>
                    <span className="branch-node-name">{node.champion && <Portrait name={node.champion} size={23} muted={node.kind === 'ban'} />} {nodeLabel(node)}</span>
                  </button>
                )
              })}
            </div>
          ))}
          {!state.completed && state.kind !== 'next_game' && !plan.nodes.some(node => node.parent_id === state.node_id) &&
            <div className="tree-tier"><span className="tier-line tier-line-new" /><button type="button" className="branch-node branch-new" disabled={busy} onClick={custom}><Plus size={17} /> Add a branch</button></div>}
        </div>
      </div>
      <div className="branch-footer">
        <span><Link2 size={14} /> {compareReady ? 'Select a node to revisit its draft.' : 'Return to a node to try a different decision.'}</span>
        {path.length > 1 && <button type="button" className="text-button" disabled={busy} onClick={() => select(path.at(-2)!.id)}>Parent node <ArrowUpRight size={13} /></button>}
      </div>
    </section>
  )
}

function DecisionPanel({ state, team, busy, commit, advance, historical, custom, pin }: {
  state: NodeState; team: string | null; busy: boolean; commit: (champion: string | null) => void
  advance: () => void; historical: boolean; custom: () => void; pin?: (champion: string) => void
}) {
  const options = state.suggestions.filter(option => state.legal.includes(option.champion))
  const [selected, setSelected] = useState(options[0]?.champion)
  const evidence = options.find(option => option.champion === selected)
  const nextHistorical = state.history
  const publicEstimate = isPublicEstimate(state.forecast)
  const showLikelihood = state.forecast?.calibrated === true || publicEstimate || isEventEstimate(state.forecast)
  const verb = kindLabel(state.kind)
  return (
    <section className="panel decision-panel" aria-labelledby="decision-title">
      <header className="panel-heading decision-header">
        <div><span className="section-kicker"><Crosshair size={15} /> Next decision</span><h2 id="decision-title">{state.completed ? 'Series complete' : `${team} ${verb.toLowerCase()}`}</h2></div>
        {!state.completed && <span className="turn-badge">TURN {String(state.actions.length + 1).padStart(2, '0')}</span>}
      </header>
      {state.completed ? <div className="decision-empty muted">Choose a node in the branch map to revisit the series.</div> : (
        <>
          {nextHistorical && historical && (
            <button type="button" className="historical-step" onClick={advance} disabled={busy}>
              <Play size={15} /><span>Replay historical {nextHistorical.kind}<strong>{nextHistorical.champion ?? 'Pass'}</strong></span><ChevronRight size={16} />
            </button>
          )}
          <div className="suggestion-heading">
            <span><Sparkles size={14} /> Ranked {verb.toLowerCase()} options</span>
            <span title="All champions legal for this draft position">{state.legal.length} legal</span>
          </div>
          {options.length > 0 ? (
            <div className="suggestion-list" aria-label="Legal ranked options">
              {options.map((option, index) => (
                <button type="button" key={option.champion} className={`suggestion-row ${selected === option.champion ? 'suggestion-selected' : ''}`}
                  aria-pressed={selected === option.champion} onClick={() => setSelected(option.champion)}>
                  <span className="suggestion-rank">{String(index + 1).padStart(2, '0')}</span>
                  <Portrait name={option.champion} size={34} />
                  <span className="suggestion-title"><strong>{option.champion}</strong>{option.roles?.length ? <small>{option.roles.join(' / ')}</small> : null}</span>
                  {showLikelihood && option.score != null && <span className="suggestion-score" title={publicEstimate ? 'Public estimate conditional on a named champion action' : 'Likelihood conditional on a named champion action'}>{probabilityLabel(option.score)}</span>}
                  <ChevronRight size={15} />
                </button>
              ))}
            </div>
          ) : <div className="decision-empty muted">No ranked options for this position. Search the legal champion pool.</div>}
          {evidence && (
            <div className="evidence-card">
              <div className="evidence-top"><span><ShieldCheck size={14} /> Legal now</span><span>{evidence.games == null ? 'Sample unavailable' : `${evidence.games} ${evidence.games === 1 ? 'game' : 'games'}`}</span></div>
              <p>{evidence.reason}</p>
              <div className="evidence-source">{state.source}</div>
              <div className={`evidence-actions ${pin ? "with-pin" : ""}`}>
                <button type="button" className="button button-accent" onClick={() => commit(evidence.champion)} disabled={busy}><ActionIcon kind={state.kind} /> {verb} {evidence.champion}<ArrowRight size={15} /></button>
                {pin && <button type="button" className="icon-button button-secondary" onClick={() => pin(evidence.champion)} aria-label={`Pin ${evidence.champion} for a future game`} title="Pin for a future game"><PinIcon size={16} /></button>}
              </div>
            </div>
          )}
          <div className="decision-footer">
            <button type="button" className="button button-small button-quiet" onClick={custom} disabled={busy || !state.legal.length}><ListFilter size={15} /> Custom champion</button>
            {state.kind === 'ban' && <button type="button" className="text-button" onClick={() => commit(null)} disabled={busy}>Pass ban <ArrowRight size={13} /></button>}
          </div>
        </>
      )}
    </section>
  )
}

function PinPanel({ pins, add, maxGame, game }: { pins: Pin[]; add: () => void; maxGame: number; game: number }) {
  return (
    <section className="panel pin-panel" aria-labelledby="pin-title">
      <header className="panel-heading">
        <div><span className="section-kicker"><PinIcon size={15} /> Series prep</span><h2 id="pin-title">Future-game pins</h2></div>
        <button type="button" className="button button-small button-secondary" disabled={game >= maxGame} onClick={add}><Plus size={16} /> Add pin</button>
      </header>
      {pins.length ? <div className="pin-list">{pins.map(item => (
        <div className="pin-row" key={`${item.game}-${item.player}-${item.champion}`}>
          <Portrait name={item.champion} size={36} />
          <div className="pin-copy"><strong>{item.champion}</strong><span>Game {item.game} · {item.player}</span><p>{item.reason}{item.backup && ` Backup: ${item.backup}.`}</p></div>
          <span className={`pin-status ${pinTone(item.status)}`}>{item.status.replaceAll('_', ' ')}</span>
        </div>
      ))}</div> : (
        <div className="pin-empty"><span className="pin-empty-icon"><PinIcon size={18} /></span><p>Save a priority champion for a later game and track its availability across branches.</p></div>
      )}
    </section>
  )
}

function pinTone(status: string) {
  if (status === 'conflict' || status === 'locked') return 'pin-conflict'
  if (status === 'still_possible' || status === 'at_risk') return 'pin-warn'
  return ''
}

export function RulesContent({ state }: { state: NodeState }) {
  return (
    <div className="rules-content">
      <div><Lock size={19} /><section><h3>Across games</h3><p>A champion picked by either team is locked for both teams for the rest of the series. Bans expire at the end of each game.</p></section></div>
      <div><Workflow size={19} /><section><h3>In this draft</h3><p>Each picked or banned champion leaves the current legal pool. A passed ban uses the turn and leaves all champions available.</p></section></div>
      <div><ArrowDown size={19} /><section><h3>At this node</h3><LockCount count={state.locked.length} /><div className="champion-chips">{state.locked.map(champion => <span key={champion}>{champion}</span>)}</div></section></div>
    </div>
  )
}
