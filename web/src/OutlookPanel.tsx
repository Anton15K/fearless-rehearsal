import { ChevronDown, Telescope } from 'lucide-react'
import { Portrait } from './common'
import { kindLabel } from './draft'
import { isPublicEstimate, modeledOutlook, probabilityLabel, type Outlook } from './outlook'

export function OutlookPanel({ outlook, unavailable }: { outlook?: Outlook | null; unavailable: boolean }) {
  const forecast = modeledOutlook(outlook)
  const publicEstimate = isPublicEstimate(forecast)
  const projection = forecast ?? (outlook?.status === 'scored' && outlook.paths.length ? outlook : null)
  if ((!outlook && !unavailable) || outlook?.status === 'complete') return null

  return (
    <section className="panel outlook-panel" aria-labelledby="outlook-title">
      <header className="panel-heading">
        <div><span className="section-kicker"><Telescope size={15} /> Lookahead</span><h2 id="outlook-title">Opponent outlook</h2></div>
        {forecast && <span className="turn-badge">{forecast.horizon} {forecast.horizon === 1 ? 'action' : 'actions'}</span>}
      </header>
      {!projection ? (
        <p className="outlook-copy muted">{outlook?.status === 'awaiting_coach'
          ? 'Choose your draft action to see the opponent’s next modeled reply.'
          : 'Probability estimates are unavailable. See ranked options for the immediate opponent reply.'}</p>
      ) : (
        <div className="outlook-body">
          {forecast ? <>
            <p className="outlook-copy">{publicEstimate && 'Public estimate from earlier professional drafts. '}Conditional on named champion choices. The outlook stops before your next decision.</p>
            <div className="mass-key">
              <span><i className="modeled-swatch" /> {publicEstimate ? 'Estimated path mass' : 'Modeled path mass'} {probabilityLabel(forecast.observed_mass)}</span>
              <span><i className="residual-swatch" /> Residual {probabilityLabel(forecast.residual_mass)}</span>
            </div>
            <div className="mass-track" aria-hidden="true"><div style={{ width: `${forecast.observed_mass * 100}%` }} /></div>
          </> : <p className="outlook-copy">Leading ranked replies. Probability estimates are unavailable at this position.</p>}
          {projection.pairs.length > 0 && (
            <div className="outlook-group">
              <h3>Leading pick pairs · either order</h3>
              <ul className="outlook-list">
                {projection.pairs.slice(0, 3).map(pair => (
                  <li key={pair.champions.join(' / ')}>
                    <span className="outlook-pair">{pair.champions.map(name => <span key={name}><Portrait name={name} size={23} />{name}</span>)}</span>
                    {forecast && <span className="path-mass">{probabilityLabel(pair.probability)}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {forecast && forecast.outcomes.length > 0 && (
            <div className="outlook-group">
              <h3>{forecast.champion ? `${forecast.champion} pin` : 'Pinned champion'} · at this horizon</h3>
              <ul className="outlook-list outcome-list">
                {forecast.outcomes.map(outcome =>
                  <li key={outcome.status}><span>{outcome.status.replaceAll('_', ' ')}</span><span className="path-mass">{probabilityLabel(outcome.probability)}</span></li>,
                )}
              </ul>
            </div>
          )}
          <details className="outlook-paths" open={!projection.pairs.length}>
            <summary><span>{projection.paths.length} {forecast ? 'modeled' : 'ranked'} {projection.paths.length === 1 ? 'path' : 'paths'}</span><ChevronDown size={15} /></summary>
            <ul className="outlook-list">
              {projection.paths.map((path, index) => (
                <li key={index}>
                  <span className="outlook-path">{path.actions.map((action, step) => (
                    <span key={step}><span className="path-step">{action.team} · {kindLabel(action.kind)}</span><strong>{action.champion ?? 'Pass'}</strong></span>
                  ))}</span>
                  {forecast && <span className="path-mass">{probabilityLabel(path.probability)}</span>}
                </li>
              ))}
            </ul>
          </details>
        </div>
      )}
    </section>
  )
}
