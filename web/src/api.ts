import type { Action, DraftNode } from './draft'

export type Match = {
  id: string
  title: string
  teams: string[]
  patch: string
  date: string
  format?: string
  source?: string
  rosters?: Record<string, string[] | Record<string, string>>
}

export type PlanInput = {
  match_id: string
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
}

export type Suggestion = {
  champion: string
  score?: number | null
  reason: string
  games?: number | null
  roles?: string[]
}

export type NodeState = {
  node_id: string
  game: number
  first: string
  second: string
  team: string | null
  kind: string
  actions: Action[]
  locked: string[]
  legal: string[]
  suggestions: Suggestion[]
  pins: Pin[]
  source: string
  completed?: boolean
  history?: { team?: string; kind?: string; champion?: string | null; next?: Action | null } | null
}

async function request<T>(path: string, data?: unknown, signal?: AbortSignal): Promise<T> {
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
  advance: (id: string, parent_id: string) =>
    request<Plan>(`/plans/${id}/advance-history`, { parent_id }),
  pin: (id: string, data: { champion: string; game: number; player: string; backup?: string }) =>
    request<Plan>(`/plans/${id}/pins`, data),
  brief: (id: string, data: BriefNote) => request<Plan>(`/plans/${id}/brief`, data),
}
