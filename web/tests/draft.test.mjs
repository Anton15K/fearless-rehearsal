import test from 'node:test'
import assert from 'node:assert/strict'
import { branchPath, visibleBranches, slotGroups } from '../src/draft.ts'

const nodes = [
  { id: 'root', parent_id: null, game: 2, kind: 'root', team: null, champion: null },
  { id: 'a', parent_id: 'root', game: 2, kind: 'ban', team: 'Blue', champion: 'Ahri' },
  { id: 'b', parent_id: 'root', game: 2, kind: 'ban', team: 'Blue', champion: 'Braum' },
  { id: 'c', parent_id: 'a', game: 2, kind: 'ban', team: 'Red', champion: 'Camille' },
  { id: 'd', parent_id: 'c', game: 2, kind: 'ban', team: 'Blue', champion: 'Diana' },
  { id: 'e', parent_id: 'd', game: 3, kind: 'start', team: 'Blue', champion: null },
]
const ids = entries => entries.map(entry => entry.id)

test('selecting a saved leaf keeps its own history', () => {
  assert.deepEqual(ids(branchPath(nodes, 'c')), ['root', 'a', 'c'])
  assert.deepEqual(ids(branchPath(nodes, 'b')), ['root', 'b'])
})

test('branch map keeps alternate replies on the selected path', () => {
  assert.deepEqual(ids(visibleBranches(nodes, 'c').flat()), ['root', 'a', 'b', 'c', 'd'])
})

test('draft slots preserve two ban and pick phases for each team', () => {
  const actions = [
    { kind: 'ban', team: 'Blue', champion: 'Ahri' },
    { kind: 'ban', team: 'Red', champion: 'Braum' },
    { kind: 'pick', team: 'Blue', champion: 'Camille' },
  ]
  assert.deepEqual(slotGroups(actions, 'Blue').map(group => group.map(slot => slot?.champion || null)), [
    ['Ahri', null, null],
    ['Camille', null, null],
    [null, null],
    [null, null],
  ])
})
