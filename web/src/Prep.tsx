import { useState, type FormEvent } from 'react'
import { ArrowRight, CalendarDays, ChevronRight, ClipboardList, FolderOpen, Plus, Swords } from 'lucide-react'
import type { Bootstrap, Match, Plan, PlanInput, PlanSummary } from './api'

export const matchFormat = (match?: Match | Plan) => match?.format || 'Bo5'

export function MatchPrep({ bootstrap, plan, plans, busy, create, open }: {
  bootstrap: Bootstrap
  plan: Plan
  plans: PlanSummary[]
  busy: boolean
  create: (data: PlanInput) => void
  open: (id: string) => void
}) {
  const [id, setId] = useState(plan.match_id || bootstrap.default_match)
  const match = bootstrap.matches.find(candidate => candidate.id === id) ?? bootstrap.matches[0]
  if (!match) return <section className="panel empty">No matches are available.</section>

  return (
    <div className="prep-grid">
      <section className="panel prep-panel" aria-labelledby="match-select-title">
        <header className="panel-heading"><div><span className="section-kicker"><Swords size={15} /> Series</span><h2 id="match-select-title">Choose a matchup</h2></div></header>
        <div className="match-options">
          {bootstrap.matches.map(option => (
            <button key={option.id} type="button" className={`match-option ${option.id === match.id ? 'is-selected' : ''}`} aria-pressed={option.id === match.id} onClick={() => setId(option.id)}>
              <div className="match-option-copy"><span className={`source-label ${option.source === 'historical' ? 'source-historical' : ''}`}>{option.source === 'historical' ? 'Historical replay' : 'Illustrative scenario'}</span>
              <strong>{option.title}</strong>
              <span className="match-option-meta">{matchFormat(option)} <i /> Patch {option.patch}{option.date && <> <i /> <CalendarDays size={13} /> {option.date}</>}</span></div>
              <ChevronRight size={19} />
            </button>
          ))}
        </div>
        <div className="saved-plans">
          <h3><FolderOpen size={16} /> Saved plans</h3>
          {plans.length ? plans.map(saved => (
            <button key={saved.id} type="button" disabled={busy} className="saved-plan" onClick={() => open(saved.id)}>
              <span>{saved.title}{saved.id === plan.id && <small>Current</small>}</span><ArrowRight size={16} />
            </button>
          )) : <p className="muted">Your rehearsal plans will appear here.</p>}
        </div>
      </section>
      <SetupForm key={match.id} match={match} busy={busy} create={create} />
    </div>
  )
}

function SetupForm({ match, busy, create }: { match: Match; busy: boolean; create: (input: PlanInput) => void }) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const get = (key: string) => String(data.get(key) ?? '').trim()
    const rosters = Object.fromEntries(match.teams.flatMap((team, index) => {
      const players = get(`roster-${index}`).split(',').map(name => name.trim()).filter(Boolean)
      return players.length ? [[team, players]] : []
    }))

    create({
      match_id: match.id,
      title: get('title') || undefined,
      priority: get('priority') || undefined,
      target_game: Number(get('game')),
      player: get('player') || undefined,
      patch: get('patch') || undefined,
      first: get('first'),
      rosters: Object.keys(rosters).length ? rosters : undefined,
    })
  }

  return (
    <section className="panel prep-panel" aria-labelledby="setup-title">
      <header className="panel-heading"><div><span className="section-kicker"><ClipboardList size={15} /> Setup</span><h2 id="setup-title">Rehearsal settings</h2></div></header>
      <form className="form-grid" onSubmit={submit}>
        <label className="field wide">Plan name <input name="title" placeholder={match.title} maxLength={120} /></label>
        <label className="field wide">Series priority <input name="priority" placeholder="e.g. Preserve a comfort pick for Game 3" maxLength={200} /></label>
        <label className="field">Priority game <select name="game" defaultValue="3">{Array.from({ length: Number(matchFormat(match).slice(-1)) || 5 }, (_, index) => (
          <option key={index} value={index + 1}>Game {index + 1}</option>
        ))}</select></label>
        <label className="field">Priority player <input name="player" placeholder="e.g. Mid laner" maxLength={80} /></label>
        <label className="field">Patch <input name="patch" defaultValue={match.patch} required maxLength={30} /></label>
        <label className="field">First pick <select name="first">{match.teams.map(team => <option key={team}>{team}</option>)}</select></label>
        {match.teams.map((team, index) => {
          const roster = match.rosters?.[team]
          const players = Array.isArray(roster) ? roster : Object.values(roster ?? {})
          return <label className="field" key={team}>{team} roster <input name={`roster-${index}`} defaultValue={players.join(', ')} placeholder="Names, separated by commas" /></label>
        })}
        <div className="wide form-actions"><button type="submit" className="button button-accent" disabled={busy}><Plus size={17} /> Create rehearsal <ArrowRight size={16} /></button></div>
      </form>
    </section>
  )
}
