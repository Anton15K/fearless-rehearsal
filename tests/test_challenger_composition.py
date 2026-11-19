import gzip
import json

import numpy as np
import pytest

from fearless.api import _forecaster
from fearless.ban_challenger import METHOD as BAN_METHOD, BanChallenger
from fearless.calibration import CalibrationPolicy, CalibrationSidecar, ScoredPick, bundle_fingerprint
from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.model import BUNDLE_VERSION, DraftForecaster
from fearless.model_features import FEATURES, ChampionCatalog
from fearless.outlook import lookahead
from fearless.rehearsal import Position, _opponent_outlook, forecast_at
from fearless.rules import schedule


CHOICES = {
    None: {'Ahri': 0.55, 'Braum': 0.25, 'Caitlyn': 0.12, 'Diana': 0.08},
    'Ahri': {'Braum': 0.49, 'Caitlyn': 0.26, 'Diana': 0.25},
    'Braum': {'Caitlyn': 0.85, 'Ahri': 0.10, 'Diana': 0.05},
    'Caitlyn': {'Ahri': 0.85, 'Braum': 0.10, 'Diana': 0.05},
    'Diana': {'Ahri': 0.90, 'Braum': 0.06, 'Caitlyn': 0.04},
}
FILLERS = ['Evelynn', 'Fizz', 'Galio', 'Hecarim', 'Irelia', 'Janna', 'Karma']
POOL = FILLERS + list(CHOICES[None])


class FrozenChoices:
    """Deterministic conditional values for a real lookahead and calibration replay."""

    def __init__(self, calibrated=True):
        self.catalog = ChampionCatalog({name: '' for name in POOL})
        self.calibrated = calibrated

    def forecast(self, *, current, limit, **context):
        ordered = CHOICES[current[-1]['champion'] if len(current) == 8 else None]
        return {'status': 'scored', 'calibrated': self.calibrated, 'calibration_through': '2026-03-27',
                'as_of': '2026-08-31', 'method': 'learned', 'legal_candidates': len(ordered),
                'choices': [{'champion': name, 'probability': probability, 'evidence': []}
                            for name, probability in list(ordered.items())[:limit]]}


def calibration(model_id=None):
    row = ScoredPick('2026-09-01', '2026-09-01', '16.17', 'previous-series', 'previous-game',
                     'later', (0.95, 0.049, 0.001), 2, position=6)
    return CalibrationSidecar.fit([row], through='2026-09-02', model_id=model_id,
                                  policy=CalibrationPolicy(min_series=1, min_turns=1))


def pair_position():
    current = [{'team': team, 'kind': kind, 'champion': name}
               for name, (team, kind) in zip(FILLERS, schedule('Blue', 'Red'))]
    plan = {'date': '2026-09-05', 'patch': '16.17', 'region': 'EMEA', 'pool': POOL, 'rosters': {},
            'team_ids': {'Blue': 'team-blue', 'Red': 'team-red'}, 'coach_team': 'Blue', 'pins': [],
            'target_game': 2, 'player': None}
    return plan, Position({'game': 1}, 'Blue', 'Red', [current], current, set(), 'Red', 'pick')


def test_calibration_corrects_the_full_legal_distribution_before_shortlisting():
    plan, pos = pair_position()
    frozen = FrozenChoices()
    wrapper = DraftChallengers(frozen, calibration=calibration())
    all_choices = forecast_at(plan, pos, wrapper, pos.current, limit=4)
    shortlisted = forecast_at(plan, pos, wrapper, pos.current, limit=2)
    temperature = all_choices['pick_calibration']['temperature']
    weights = [probability ** (1 / temperature) for probability in CHOICES[None].values()]

    assert temperature > 1.0
    assert [choice['champion'] for choice in all_choices['choices']] == list(CHOICES[None])
    assert [choice['probability'] for choice in all_choices['choices']] == pytest.approx(
        [weight / sum(weights) for weight in weights])
    assert sum(choice['probability'] for choice in all_choices['choices']) == pytest.approx(1.0)
    assert shortlisted['choices'] == all_choices['choices'][:2]
    assert shortlisted['calibration_through'] == '2026-09-01'


def test_calibration_alone_preserves_frozen_pair_order_and_path_mass():
    plan, pos = pair_position()
    frozen = FrozenChoices()
    wrapper = DraftChallengers(frozen, calibration=calibration())
    original_pick = forecast_at(plan, pos, frozen, pos.current, limit=3)
    corrected_pick = forecast_at(plan, pos, wrapper, pos.current, limit=3)
    original = _opponent_outlook(plan, pos, frozen, original_pick)
    assert original['pairs'][0]['champions'] == ['Ahri', 'Braum']
    # Feeding the corrected rows into the pair beam would actually change the winner.
    recalculated = lookahead(pool=POOL, locked=(), current=pos.current, first=pos.first, second=pos.second,
                             coach_team=plan['coach_team'],
                             forecast=lambda prefix: forecast_at(plan, pos, wrapper, prefix, limit=3))
    assert recalculated['pairs'][0]['champions'] == ['Ahri', 'Caitlyn']

    composed = _opponent_outlook(plan, pos, wrapper, corrected_pick)
    assert corrected_pick['choices'][0]['probability'] < original_pick['choices'][0]['probability']
    assert composed == original


def test_pair_ranking_is_an_explicit_alternative_against_frozen_picks():
    plan, pos = pair_position()
    frozen = FrozenChoices()

    class PairAlternatives:
        def rank_pairs(self, *, forecaster, **context):
            assert forecaster is frozen
            assert [action['team'] for action in context['current']] == [
                plan['team_ids'][action['team']] for action in pos.current]
            return [{'champions': ['Caitlyn', 'Diana']}]

    wrapper = DraftChallengers(frozen, calibration=calibration(), pair=PairAlternatives())
    original = _opponent_outlook(plan, pos, frozen, forecast_at(plan, pos, frozen, pos.current, limit=3))
    composed = _opponent_outlook(plan, pos, wrapper, forecast_at(plan, pos, wrapper, pos.current, limit=3))
    assert composed == {**original, 'ranked_pair_alternatives': [{'champions': ['Caitlyn', 'Diana']}]}


def test_calibration_leaves_bans_and_uncalibrated_picks_unchanged():
    plan, pos = pair_position()
    frozen = FrozenChoices(calibrated=False)
    wrapper = DraftChallengers(frozen, calibration=calibration())
    for prefix in (pos.current, pos.current[:5]):
        assert (forecast_at(plan, pos, wrapper, prefix, limit=2)
                == forecast_at(plan, pos, frozen, prefix, limit=2))

    unsupported_plan = {**plan, 'patch': '16.20'}
    assert (forecast_at(unsupported_plan, pos, wrapper, pos.current, limit=2)
            == forecast_at(unsupported_plan, pos, frozen, pos.current, limit=2))


def test_ban_sidecar_routing_preserves_opening_bans_and_picks():
    catalog = ChampionCatalog({f'Champion {number:02d}': '' for number in range(26)})
    frozen = DraftForecaster(catalog, [])

    class DirectedRanker:
        def predict(self, values, *, thread_count):
            return np.arange(len(values), dtype=np.float64)

    ban = BanChallenger(frozen, DirectedRanker(), {'weight': 0.7, 'challenger_temperature': 1.0, 'temperature': 1.0})
    wrapper = DraftChallengers(frozen, ban=ban)
    actions = [{'team': team, 'kind': kind, 'champion': name}
               for name, (team, kind) in zip(catalog.names, schedule('Blue', 'Red'))]
    context = dict(date='2026-09-05', patch='16.17', region='EMEA', first='Blue', second='Red', game_number=1)
    for position in (5, 7, 16):
        assert wrapper.forecast(**context, current=actions[:position]) == frozen.forecast(**context, current=actions[:position])

    result = wrapper.forecast(**context, current=actions[:12])
    assert result['method'] == BAN_METHOD
    assert result['choices'][0]['champion'] == 'Champion 25'


@pytest.fixture
def private_policy(tmp_path):
    bundle = tmp_path / 'frozen'
    bundle.mkdir()
    manifest = {'version': BUNDLE_VERSION, 'features': list(FEATURES), 'models': {}, 'serving': {},
                'catalog': ChampionCatalog({name: '' for name in POOL}).to_json()}
    (bundle / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    with gzip.open(bundle / 'history.jsonl.gz', mode='wt', encoding='utf8') as stream:
        stream.write('')
    calibration_file = tmp_path / 'calibration.json'
    calibration(bundle_fingerprint(bundle)).save(calibration_file)
    policy = tmp_path / 'policy.json'
    config = {'version': 1, 'incumbent': incumbent_signature(bundle), 'components': {'calibration': calibration_file.name}}
    policy.write_text(json.dumps(config), encoding='utf8')
    return bundle, policy, config, calibration_file


def test_private_policy_opt_in_loads_only_the_named_component(private_policy, monkeypatch):
    bundle, policy, _, _ = private_policy
    monkeypatch.delenv('FEARLESS_CHALLENGERS', raising=False)
    assert isinstance(_forecaster(bundle), DraftForecaster)
    monkeypatch.setenv('FEARLESS_CHALLENGERS', str(policy))
    composed = _forecaster(bundle)
    assert isinstance(composed, DraftChallengers)
    assert composed.calibration is not None
    assert composed.pair is None
    assert composed.ban is None


@pytest.mark.parametrize('damage', ['fingerprint', 'unknown component', 'bad root', 'bad path', 'bad sidecar',
                                    'invalid patch', 'changed component'])
def test_bad_private_policy_falls_back_to_the_original_frozen_bundle(private_policy, monkeypatch, damage):
    bundle, policy, config, calibration_file = private_policy
    if damage == 'fingerprint':
        config['incumbent'] = 'different-model'
    elif damage == 'unknown component':
        config['components']['mystery'] = 'anything.json'
    elif damage == 'bad root':
        config = []
    elif damage == 'bad path':
        config['components']['calibration'] = ['bad-path-type']
    elif damage == 'bad sidecar':
        sidecar = json.loads(calibration_file.read_text(encoding='utf8'))
        sidecar['series'] = None
        calibration_file.write_text(json.dumps(sidecar), encoding='utf8')
    elif damage == 'invalid patch':
        sidecar = json.loads(calibration_file.read_text(encoding='utf8'))
        sidecar['series'][0]['patch'] = 'unknown'
        calibration_file.write_text(json.dumps(sidecar), encoding='utf8')
    elif damage == 'changed component':
        config['component_sha256'] = {'calibration': {calibration_file.name: 'old checksum'}}
    policy.write_text(json.dumps(config), encoding='utf8')
    monkeypatch.setenv('FEARLESS_CHALLENGERS', str(policy))

    fallback = _forecaster(bundle)
    assert isinstance(fallback, DraftForecaster)
    assert fallback.catalog.names == tuple(sorted(POOL))
