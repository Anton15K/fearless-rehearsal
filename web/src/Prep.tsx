import { useState, type FormEvent } from 'react'
import { ArrowRight, CalendarDays, ChevronRight, ClipboardList, FolderOpen, Plus, Swords } from 'lucide-react'
import type { Bootstrap, Match, Plan, PlanInput, PlanSummary } from './api'

const NEW_MATCHUP = 'prospective'
const rosterNames = (value: string) => value.split(',').map(name => name.trim()).filter(Boolean)

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
  const isNew = id === NEW_MATCHUP || !match
  const patches = bootstrap.patches ?? [...new Set(bootstrap.matches.map(option => option.patch))]

  return (
    <div className="prep-grid">
      <section className="panel prep-panel" aria-labelledby="match-select-title">
        <header className="panel-heading"><div><span className="section-kicker"><Swords size={15} /> Series</span><h2 id="match-select-title">Choose a matchup</h2></div></header>
        <div className="match-options">
          <button type="button" className={`match-option new-match-option ${isNew ? 'is-selected' : ''}`} aria-pressed={isNew} onClick={() => setId(NEW_MATCHUP)}>
            <span className="new-match-icon"><Plus size={20} /></span>
            <span className="match-option-copy"><strong>New matchup</strong><span className="match-option-meta">Start from Game 1</span></span>
            <ChevronRight size={19} />
          </button>
          {bootstrap.matches.map(option => (
            <button key={option.id} type="button" className={`match-option ${!isNew && option.id === match?.id ? 'is-selected' : ''}`} aria-pressed={!isNew && option.id === match?.id} onClick={() => setId(option.id)}>
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
      {isNew ? <NewMatchForm events={bootstrap.events ?? []} patches={patches} busy={busy} create={create} /> :
        <SetupForm key={match.id} match={match} busy={busy} create={create} />}
    </div>
  )
}

function SetupForm({ match, busy, create }: { match: Match; busy: boolean; create: (input: PlanInput) => void }) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const get = (key: string) => String(data.get(key) ?? '').trim()
    const rosters = Object.fromEntries(match.teams.flatMap((team, index) => {
      const players = rosterNames(get(`roster-${index}`))
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
        <label className="field">Priority game <select name="game" aria-label="Priority game" defaultValue="3">{Array.from({ length: Number(matchFormat(match).slice(-1)) || 5 }, (_, index) => (
          <option key={index} value={index + 1}>Game {index + 1}</option>
        ))}</select></label>
        <label className="field">Priority player <input name="player" placeholder="e.g. Mid laner" maxLength={80} /></label>
        <label className="field">Patch <input name="patch" defaultValue={match.patch} required maxLength={30} /></label>
        <label className="field">First pick <select name="first" aria-label="First pick" defaultValue={match.first}>{match.teams.map(team => <option key={team}>{team}</option>)}</select></label>
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


function NewMatchForm({ events, patches, busy, create }: { events: string[]; patches: string[]; busy: boolean; create: (input: PlanInput) => void }) {
  const [left, setLeft] = useState('')
  const [right, setRight] = useState('')
  const teams: [string, string] = [left.trim(), right.trim()]
  const labels = teams.map((name, index) => name || `Team ${index + 1}`)
  const defaultDate = new Intl.DateTimeFormat('sv-SE').format(new Date())

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const get = (key: string) => String(form.get(key) ?? '').trim()
    const rosters = Object.fromEntries(teams.flatMap((team, index) => {
      const players = rosterNames(get(`roster-${index}`))
      return players.length ? [[team, players]] : []
    }))
    create({
      match_id: NEW_MATCHUP,
      event: get('event') || undefined,
      teams,
      coach_team: teams[get('coach') === '1' ? 1 : 0],
      first: teams[get('first') === '1' ? 1 : 0],
      date: get('date'),
      format: get('format') === 'Bo3' ? 'Bo3' : 'Bo5',
      patch: get('patch'),
      rosters: Object.keys(rosters).length ? rosters : undefined,
    })
  }

  return (
    <section className="panel prep-panel" aria-labelledby="new-matchup-title">
      <header className="panel-heading"><div><span className="section-kicker"><Swords size={15} /> Game 1</span><h2 id="new-matchup-title">New matchup</h2></div></header>
      <form className="form-grid" onSubmit={submit}>
        <label className="field">Team 1 <input value={left} onChange={event => setLeft(event.target.value)} maxLength={80} placeholder="Team name" required /></label>
        <label className="field">Team 2 <input value={right} onChange={event => setRight(event.target.value)} maxLength={80} placeholder="Team name" required /></label>
        <label className="field">Your team <select name="coach" aria-label="Your team">
          {labels.map((label, index) => <option key={index} value={index}>{label}</option>)}
        </select></label>
        <label className="field">First pick <select name="first" aria-label="First pick">
          {labels.map((label, index) => <option key={index} value={index}>{label}</option>)}
        </select></label>
        <label className="field">As-of date <input name="date" type="date" defaultValue={defaultDate} aria-describedby="matchup-date-help" required /></label>
        <label className="field">Series format <select name="format" aria-label="Series format" defaultValue="Bo5"><option value="Bo3">Best of 3</option><option value="Bo5">Best of 5</option></select></label>
        <label className="field">Patch <select name="patch" aria-label="Patch" required defaultValue=""><option value="">Choose a patch</option>{patches.map(patch => <option key={patch}>{patch}</option>)}</select></label>
        <p className="match-date-help muted" id="matchup-date-help">Historical evidence ends before the as-of date.</p>
        {events.length > 0 && <label className="field wide">Tournament <select name="event" defaultValue="">
          <option value="">Use patch history</option>{events.map(name => <option key={name}>{name}</option>)}
        </select></label>}
        <details className="matchup-rosters wide">
          <summary>Optional rosters</summary>
          <div className="form-grid">
            {labels.map((name, index) => <label className="field" key={index}>{name} roster <input name={`roster-${index}`} placeholder="Names, separated by commas" /></label>)}
          </div>
        </details>
        <div className="wide form-actions"><button type="submit" className="button button-accent" disabled={busy || !teams.every(Boolean)}>Start Game 1 <ArrowRight size={16} /></button></div>
      </form>
    </section>
  )
}
