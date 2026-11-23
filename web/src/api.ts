import type { Action, DraftNode } from './draft'
import type { Outlook } from './outlook'

export type SelectionPolicy = 'first_selection' | 'blue_first' | 'organizer'
export type DraftMode = 'fearless' | 'standard'
export type RuleProfile = { id: string; label: string; source?: string | null }
export type Restriction = { champion: string; reason: string; source?: string | null; release_source?: string }
export type Selection = {
  first: string
  blue: string | null
  choice?: 'first_pick' | 'map_side'
  hypothetical?: boolean
  disabled?: string[]
}

export type Match = {
  id: string
  title: string
  teams: string[]
  patch: string
  date: string
  format?: string
  source?: string
  first?: string
  blue?: string | null
  rosters?: Record<string, string[] | Record<string, string>>
}

export type PlanInput = {
  match_id: string
  event?: string
  disabled?: string[]
  teams?: [string, string]
  coach_team?: string
  date?: string
  format?: 'Bo1' | 'Bo3' | 'Bo5'
  blue?: string
  ruleset?: string
  selection_policy?: SelectionPolicy
  draft_mode?: DraftMode
  title?: string
  priority?: string
  target_game?: number
  player?: string
  first?: string
  patch?: string
  rosters?: Record<string, string[] | Record<string, string>>
}

export type Bootstrap = {
  champions: string[]
  matches: Match[]
  default_match: string
  events?: string[]
  patches?: string[]
  rulesets?: RuleProfile[]
  selection_policies?: RuleProfile[]
}

export type Pin = {
  champion: string
  game: number
  player: string
  status: string
  reason: string
  backup?: string | null
}

export type BriefNote = {
  node_id: string
  text: string
  backup?: string | null
}

export type PlanSummary = {
  id: string
  title: string
  teams?: string[]
  patch?: string
  match_id?: string
}

export type Plan = PlanSummary & {
  teams: string[]
  patch: string
  date: string
  event?: string | null
  disabled?: string[]
  ruleset?: string
  selection_policy?: SelectionPolicy
  draft_mode?: DraftMode
  root_id: string
  selected_node: string
  nodes: DraftNode[]
  pins: Pin[]
  brief: BriefNote[]
  format?: string
  source?: string
  priority?: string
  target_game?: number
  player?: string
  coach_team?: string
  team_history?: Record<string, { as_of: string } | null> | null
}

export type Suggestion = {
  champion: string
  score?: number | null
  reason: string
  games?: number | null
  roles?: string[]
}

export type NodeState = {
  selection_policy?: SelectionPolicy
  draft_mode?: DraftMode
  patch?: string
  ruleset?: string
  node_id: string
  game: number
  first: string
  second: string
  blue: string | null
  red: string | null
  game_winner: string | null
  series_winner: string | null
  selection_right: string | null
  selection_choice: Selection['choice'] | null
  progression: 'actual' | 'historical' | 'hypothetical'
  series_status: 'drafting' | 'awaiting_result' | 'awaiting_selection' | 'clinched' | 'rehearsal_complete'
  can_rehearse_next: boolean
  score_complete: boolean
  score: Record<string, number>
  disabled?: string[]
  game_disabled?: string[]
  restriction_status?: string
  restrictions?: Restriction[]
  effective_pool_size?: number
  team: string | null
  kind: string
  actions: Action[]
  locked: string[]
  legal: string[]
  suggestions: Suggestion[]
  pins: Pin[]
  source: string
  completed?: boolean
  outlook?: Outlook | null
  forecast?: { calibrated?: boolean; method?: string; model_source?: string; as_of?: string; probability_quality?: string } | null
  history?: { team?: string; kind?: string; champion?: string | null; next?: Action | null } | null
}

async function request<T>(path: string, data?: unknown, signal?: AbortSignal): Promise<T> {
  if (import.meta.env.MODE === 'demo') {
    const { requestDemo } = await import('./demo')
    return requestDemo<T>(path, data, signal)
  }

  const response = await fetch(`/api${path}`, {
    method: data === undefined ? 'GET' : 'POST',
    headers: data === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
    signal,
  })

  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(typeof error?.detail === 'string' ? error.detail : `Request failed (${response.status})`)
  }

  return response.json()
}

export const api = {
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  plans: () => request<PlanSummary[]>('/plans'),
  plan: (id: string) => request<Plan>(`/plans/${id}`),
  create: (data: PlanInput) => request<Plan>('/plans', data),
  node: (id: string, node: string, signal?: AbortSignal) =>
    request<NodeState>(`/plans/${id}/nodes/${node}`, undefined, signal),
  act: (id: string, parent_id: string, champion: string | null, first?: string) =>
    request<Plan>(`/plans/${id}/actions`, { parent_id, champion, first }),
  select: (id: string, parent_id: string, data: Selection) =>
    request<Plan>(`/plans/${id}/selection`, { parent_id, ...data }),
  result: (id: string, parent_id: string, winner: string) =>
    request<Plan>(`/plans/${id}/result`, { parent_id, winner }),
  advance: (id: string, parent_id: string) =>
    request<Plan>(`/plans/${id}/advance-history`, { parent_id }),
  pin: (id: string, data: { node_id: string; champion: string; game: number; player: string; backup?: string }) =>
    request<Plan>(`/plans/${id}/pins`, data),
  brief: (id: string, data: BriefNote) => request<Plan>(`/plans/${id}/brief`, data),
}
