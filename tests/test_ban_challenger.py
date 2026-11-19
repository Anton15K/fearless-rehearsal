import numpy as np

from fearless.ban_challenger import (
    METHOD, BanChallenger, RoleHistory, evaluate, marginal_losses, role_openings, second_phase_ban, train,
)
from fearless.model import DraftForecaster, train as train_frozen
from fearless.model_features import ChampionCatalog, ROLES
from fearless.rules import schedule


def actions(length):
    return [{'team': team, 'kind': kind, 'champion': f'C{index:02d}'}
            for index, (team, kind) in enumerate(schedule('A', 'B')[:length])]


def test_only_second_phase_bans_at_deployable_dates_use_challenger():
    for position in range(21):
        assert second_phase_ban('2026-03-28', 'A', 'B', actions(position)) == (12 <= position <= 15)
    assert not second_phase_ban('2026-03-27', 'A', 'B', actions(12))


def test_distinct_role_assignment_handles_shared_flex_roles():
    likelihood = np.array([[1, 0, 0, 0, 0], [0.5, 0.5, 0, 0, 0], [0, 0, 1, 0, 0]])
    open_roles, open_pairs = role_openings(likelihood)
    np.testing.assert_array_equal(open_roles, [0, 0, 0, 1, 1])
    assert open_pairs[3, 4] == 1
    assert open_pairs[0, 1] == 0


def test_banning_the_only_comfort_pick_removes_joint_role_coverage():
    comfort = np.array([[1, 0.5, 0, 0, 0], [0, 0.5, 0, 0, 0], [0, 0, 1, 0, 0]])
    open_roles = np.array([1, 1, 0, 0, 0])
    open_pairs = np.outer(open_roles, open_roles)
    pressure, joint_loss, _, _ = marginal_losses(comfort, open_roles, open_pairs)
    np.testing.assert_allclose(pressure[:, 0], [1, 0, 0])
    np.testing.assert_allclose(joint_loss, [1, 1, 0])


def test_history_omits_roles_until_after_game_completed():
    catalog = ChampionCatalog({f'C{index:02d}': '' for index in range(30)})
    row = {'date': '2026-03-26', 'finished_at': '2026-03-27T01:00:00Z', 'eligible': True,
           'actions': actions(20), 'rosters': {'B': [{'player': 'player', 'role': 'jungle', 'champion': 'C07'}]}}
    context = RoleHistory(catalog.indices)
    context.before('2026-03-27', [])
    assert not context.population.any()
    context.before('2026-03-28', [row])
    assert context.population[0, 1, catalog.indices['C07']] == 1
    assert context.players['player'][0, 1, catalog.indices['C07']] == 1


def test_unaffected_forecasts_delegate_to_the_frozen_forecaster():
    catalog = ChampionCatalog({f'C{index:02d}': '' for index in range(30)})
    frozen = DraftForecaster(catalog, [])
    wrapper = BanChallenger(frozen, None, {})
    for position, date in [(6, '2026-04-01'), (0, '2026-04-01'), (12, '2026-03-27'), (17, '2026-04-01')]:
        args = dict(date=date, patch='16.4', region='EMEA', first='A', second='B', game_number=1,
                    current=actions(position), limit=2)
        assert wrapper.forecast(**args) == frozen.forecast(**args)


def test_round_trip_scores_every_legal_candidate_and_keeps_frozen_bundle(tmp_path):
    catalog = ChampionCatalog({f'C{index:02d}': '' for index in range(28)})
    drafts = actions(20)
    lineups = {
        team: [{'champion': pick, 'player': f'{team}-{role}', 'role': role}
               for role, pick in zip(ROLES, [action['champion'] for action in drafts
                                             if action['team'] == team and action['kind'] == 'pick'], strict=True)]
        for team in ('A', 'B')
    }
    games = [{'date': day, 'series_id': f'series-{day}', 'eligible': True, 'game_number': 1,
              'fearless': True, 'first': 'A', 'second': 'B', 'patch': '16.4', 'region': 'EMEA',
              'actions': drafts, 'rosters': lineups}
             for day in ('2025-02-02', '2025-02-05', '2026-01-10', '2026-03-27', '2026-04-15', '2026-04-17')]

    frozen = tmp_path / 'frozen'
    sidecar = tmp_path / 'sidecar'
    train_frozen(games, frozen, catalog=catalog, iterations=3, threads=1, bootstrap_samples=10)
    original = {item.name: item.read_bytes() for item in frozen.iterdir()}
    metrics = train(games, frozen, sidecar, iterations=3, threads=1, bootstrap=10)
    assert {item.name: item.read_bytes() for item in frozen.iterdir()} == original
    challenger = BanChallenger.load(sidecar, frozen)
    request = dict(date='2026-04-17', patch='16.4', region='EMEA', first='A', second='B', game_number=1,
                   current=drafts[:12], locked={'C20'}, pool=list(catalog.names), limit=28)
    forecast = challenger.forecast(**request)
    assert forecast['method'] == METHOD
    assert forecast['legal_candidates'] == len(forecast['choices']) == 15
    assert all(choice['probability'] > 0 for choice in forecast['choices'])
    np.testing.assert_allclose(sum(choice['probability'] for choice in forecast['choices']), 1.0)
    assert {choice['champion'] for choice in forecast['choices']} == set(catalog.names[12:]) - {'C20'}

    diagnostic = evaluate(games[-2:], challenger, threads=1, bootstrap=10)
    assert diagnostic['counts']['turns'] == metrics['splits']['test']['counts']['turns'] == 8
    assert diagnostic['methods'] == metrics['splits']['test']['methods']
