import type { Action } from './draft'

export type Outlook = {
  status: 'scored' | 'awaiting_coach' | 'unavailable' | 'complete'
  method: string | null
  model_source?: string
  calibrated?: boolean
  probability_quality?: string
  champion?: string
  game?: number
  player?: string
  requested_horizon: number
  horizon: number
  conditional: string
  observed_mass: number
  residual_mass: number
  outcomes: { status: string; probability: number; branches: number }[]
  ranked_pair_alternatives?: { champions: string[]; evidence?: string[] }[]
  pairs: { champions: string[]; probability: number }[]
  paths: { actions: Action[]; probability: number; pin_status?: string | null }[]
}

const statisticalMethods = new Set(['baseline', 'blend', 'learned', 'learned_meta_only', 'learned_joint', 'second_phase_ban_challenger', 'mixed'])

export function isPublicEstimate(forecast?: { method?: string | null; probability_quality?: string } | null) {
  return forecast?.method === 'public_frequencies' && forecast.probability_quality === 'estimated'
}

export function isEventEstimate(forecast?: { method?: string | null; model_source?: string; probability_quality?: string } | null) {
  return forecast?.model_source === 'private_event' && forecast.probability_quality === 'estimated'
    && ['refreshed_pick', 'patch_pick_adaptation', 'mixed'].includes(forecast.method ?? '')
}

export function modeledOutlook(outlook: Outlook | null | undefined) {
  if (outlook?.status !== 'scored' || !outlook.conditional.endsWith('opponent_actions_given_named_champion')) return null
  const calibrated = outlook.calibrated === true && outlook.method && statisticalMethods.has(outlook.method)
  if (!calibrated && !isPublicEstimate(outlook) && !isEventEstimate(outlook)) return null
  return {
    ...outlook,
    pairs: [...outlook.pairs].sort((left, right) => right.probability - left.probability),
    paths: [...outlook.paths].sort((left, right) => right.probability - left.probability),
  }
}

export function probabilityLabel(probability: number) {
  if (probability > 0 && probability < 0.001) return '<0.1%'
  return `${(probability * 100).toFixed(1)}%`
}
