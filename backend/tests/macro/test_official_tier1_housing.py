from importlib import import_module

import pytest


def document(new='下降0.4%',existing='下降0.7%',title='2026年9月份商品住宅销售价格变动情况解读'):
    return f'<html><meta charset="utf-8"><h1>{title}</h1><p>一线城市新建商品住宅销售价格环比{new}，降幅比上月收窄0.1个百分点。</p><p>一线城市二手住宅销售价格环比{existing}。二线城市二手住宅销售价格环比下降0.2%。</p><p>一线城市新建商品住宅销售价格同比下降3.1%，二手住宅销售价格同比下降6%。</p></html>'.encode()


def parse(body=None,period='2026-09'):
    return import_module('app.macro.official_macro_normalize').parse_tier1_housing(document() if body is None else body,period)


def test_official_tier1_new_and_existing_are_distinct_and_not_city_index_averages():
    value=parse();new,existing=value['points']
    assert value['source']=='nbs' and value['period']=='2026-09-01' and value['role']=='tier1-prices'
    assert (new['seriesId'],new['value'],new['housingType'])==('cn.house.price_70_city_mom.tier1','-0.4','new')
    assert (existing['seriesId'],existing['value'],existing['housingType'])==('cn.house.price_70_city_mom.tier1.existing','-0.7','existing')
    assert new['unit']==existing['unit']=='%' and new['frequency']=='monthly'
    assert all('strong_tier2' not in p['seriesId'] for p in value['points'])


@pytest.mark.parametrize('expression,expected',[
    ('上涨0.2%','0.2'),('下降0.2％','-0.2'),('持平','0'),('下降0%','0'),
    ('由上月上涨0.1%转为下降0.2%','-0.2'),('由上月持平转为上涨0.3%','0.3'),
    ('由上月下降0.4%转为持平','0'),
    ('连续两月持平','0'),('连续3个月持平','0'),
])
def test_direct_percentage_current_side_of_transition_and_real_flat(expression,expected):
    assert parse(document(new=expression))['points'][0]['value']==expected


@pytest.mark.parametrize('body',[
    document(title='2026年8月份商品住宅销售价格变动情况解读'),
    document(title='2025年9月份商品住宅销售价格变动情况解读'),
    document(new='下降0.3个百分点'),document(new='下降-0.3%'),document(new='持平0.3%'),
    document(new='下降NaN%'),document(new='下降1<br>2%'),document(new='降幅收窄0.2个百分点'),
    document().replace('一线城市'.encode(),'二线城市'.encode()),
    document()+document(new='上涨0.1%'),
    b'<script>'+document()+b'</script>',b'<html>200 error</html>',
])
def test_wrong_month_yoy_delta_other_city_group_or_ambiguous_source_rejected(body):
    with pytest.raises(ValueError):parse(body)


@pytest.mark.parametrize('label',['二手住宅销售价格','二手住宅'])
def test_existing_rate_inherits_same_sentence_tier1_subject_after_semicolon(label):
    body=document(new='上涨0.2%',existing='上涨0.7%').replace(
        '个百分点。</p><p>一线城市二手住宅销售价格'.encode(),('个百分点；'+label).encode())
    assert [p['value'] for p in parse(body)['points']]==['0.2','0.7']


@pytest.mark.parametrize('boundary',['。','。二线城市新建商品住宅销售价格环比上涨0.1%；','；二线城市新建商品住宅销售价格环比上涨0.1%；','\n'])
def test_implicit_existing_subject_cannot_cross_sentence_or_city_tier(boundary):
    body=document(new='上涨0.2%',existing='上涨0.7%').replace(
        '个百分点。</p><p>一线城市二手住宅'.encode(),('个百分点'+boundary+'二手住宅').encode())
    with pytest.raises(ValueError):parse(body)
