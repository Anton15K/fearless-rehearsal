import test from 'node:test'
import assert from 'node:assert/strict'
import { modeledOutlook, probabilityLabel } from '../src/outlook.ts'

const projection = {
  status: 'scored',
  method: 'learned_joint',
  calibrated: true,
  requested_horizon: 2,
  horizon: 2,
  conditional: 'opponent_actions_given_named_champion',
  observed_mass: 0.45,
  residual_mass: 0.55,
  outcomes: [],
  pairs: [],
  paths: [
    { actions: [{ team: 'Opponent', kind: 'pick', champion: 'Poppy' }], probability: 0.15 },
    { actions: [{ team: 'Opponent', kind: 'pick', champion: 'Vi' }], probability: 0.3 },
  ],
}

test('shows conditional statistical mass and orders represented paths by likelihood', () => {
  const ranked = modeledOutlook(projection)
  assert.equal(ranked.paths[0].actions[0].champion, 'Vi')
  assert.equal(ranked.observed_mass, 0.45)
  assert.equal(ranked.residual_mass, 0.55)
  assert.equal(probabilityLabel(0.00041), '<0.1%')
})

test('withholds probability labels for rules-only or illustrative outputs', () => {
  assert.equal(modeledOutlook({ ...projection, method: 'illustrative' }), null)
  assert.equal(modeledOutlook({ ...projection, calibrated: false }), null)
  assert.equal(modeledOutlook({ ...projection, method: null, conditional: 'rules_only' }), null)
  assert.equal(modeledOutlook({ ...projection, status: 'awaiting_coach' }), null)
})
