from vedit.analyze import Range, keep_ranges, select_expression
from vedit.compile import atempo_chain
import pytest


def test_keep_ranges_inverts_and_pads():
    silences = [Range(1.0, 3.0), Range(8.0, 10.0)]
    keeps = keep_ranges(10.0, silences, pad=0.2, min_keep=0.05)
    assert keeps[0].start == 0.0
    assert keeps[0].end == pytest.approx(1.2)
    assert keeps[1].start == pytest.approx(2.8)
    assert keeps[1].end == pytest.approx(8.2)
    assert keeps[2].start == pytest.approx(9.8)
    assert keeps[2].end == pytest.approx(10.0)


def test_keep_ranges_all_speech():
    keeps = keep_ranges(5.0, [], pad=0.1)
    assert len(keeps) == 1
    assert keeps[0].start == 0.0
    assert keeps[0].end == 5.0


def test_select_expression():
    expr = select_expression([Range(0, 1.25), Range(3, 4)])
    assert expr == "between(t,0.000,1.250)+between(t,3.000,4.000)"


def test_select_expression_rejects_empty():
    with pytest.raises(Exception):
        select_expression([])


def test_atempo_chain_fast():
    chain = atempo_chain(4.0)
    assert "atempo=2.0" in chain
    assert chain.count("atempo=") >= 2


def test_atempo_chain_slow():
    chain = atempo_chain(0.25)
    assert "atempo=0.5" in chain
