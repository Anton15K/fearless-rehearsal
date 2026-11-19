export type Action = {
  team: string | null
  kind: string
  champion: string | null
  origin?: 'historical' | 'rehearsal'
}

export type DraftNode = Action & {
  id: string
  parent_id: string | null
  game: number
}

export function branchPath(nodes: DraftNode[], selectedId: string): DraftNode[] {
  const byId = new Map(nodes.map(node => [node.id, node]))
  const path: DraftNode[] = []
  let current = byId.get(selectedId)

  while (current) {
    path.push(current)
    current = current.parent_id ? byId.get(current.parent_id) : undefined
  }

  return path.reverse()
}

export function visibleBranches(nodes: DraftNode[], selectedId: string): DraftNode[][] {
  const path = branchPath(nodes, selectedId)
  if (!path.length) return []

  const activeGame = path.at(-1)?.game
  const gamePath = path.filter(node => node.game === activeGame)
  const tiers = [[gamePath[0]]]
  for (const parent of gamePath) {
    const children = nodes.filter(node => node.parent_id === parent.id && node.game === activeGame)
    if (children.length) tiers.push(children)
  }

  return tiers
}

export function slotGroups(actions: Action[], team: string): (Action | undefined)[][] {
  const bans = actions.filter(action => action.kind === 'ban' && action.team === team)
  const picks = actions.filter(action => action.kind === 'pick' && action.team === team)
  return [
    [bans[0], bans[1], bans[2]],
    [picks[0], picks[1], picks[2]],
    [bans[3], bans[4]],
    [picks[3], picks[4]],
  ]
}

export function kindLabel(kind: string) {
  if (kind === 'ban') return 'Ban'
  if (kind === 'pick') return 'Pick'
  if (kind === 'start' || kind === 'root') return 'Opening position'
  if (kind === 'complete') return 'Series complete'
  return kind.charAt(0).toUpperCase() + kind.slice(1)
}

export function phaseLabel(kind: string, actions: Action[]) {
  if (kind === 'complete') return 'Series complete'
  const count = actions.length
  if (count < 6) return 'First ban phase'
  if (count < 12) return 'First pick phase'
  if (count < 16) return 'Second ban phase'
  return 'Second pick phase'
}

export function nodeLabel(node: DraftNode) {
  if (node.champion) return node.champion
  if (node.kind === 'ban') return 'Ban passed'
  if (node.kind === 'pick') return 'Pick'
  return `Game ${node.game} opening`
}
