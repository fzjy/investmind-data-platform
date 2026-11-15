"""REA银行经验分纯计算；输入缺口不按0，参数集中现有金融配置。"""
from decimal import Decimal,DecimalException,ROUND_HALF_UP
import json
from math import isfinite
from app.config_paths import CONFIG_ROOT

CONFIG_FILE=CONFIG_ROOT / "stocks/financial-indicators.json"
COMPONENTS=('yield','pb','large','nim','provision')

def rules():
    value=json.loads(CONFIG_FILE.read_text(encoding='utf-8'))['bankScoring']
    if value['algorithmVersion']!='rea-bank-score-20261020' or len({f['field'] for f in value['fields']})!=len(value['fields']):
        raise ValueError('BANK_RULES_INVALID')
    return value

def number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):
        return None
    try:
        exact=Decimal(str(value));approximate=float(exact)
        return exact if exact.is_finite() and isfinite(approximate) and (approximate!=0 or exact==0) else None
    except (DecimalException,ValueError,OverflowError):return None

def calc(function):
    try:return number(function())
    except (DecimalException,TypeError,ValueError,OverflowError):return None

def normalize_settings(settings):
    config=rules();divisor=number(settings.get('divisor'))
    n=config['divisor'];w=config['weight'];weights=settings.get('weights')
    if divisor is None or not n['min']<=divisor<=n['max'] or (divisor-Decimal(str(n['min'])))%Decimal(str(n['step']))!=0:
        raise ValueError('BANK_DIVISOR_INVALID')
    if not isinstance(weights,dict) or set(weights)!=set(COMPONENTS):raise ValueError('BANK_WEIGHTS_INVALID')
    normalized={}
    for key,value in weights.items():
        value=number(value)
        if value is None or not w['min']<=value<=w['max']:raise ValueError('BANK_WEIGHTS_INVALID')
        step=Decimal(str(w['normalizationStep']))
        normalized[key]=float((value/step).quantize(Decimal(1),rounding=ROUND_HALF_UP)*step)
    return dict(settings,divisor=float(divisor),weights=normalized)

def score_bank(inputs,settings):
    settings=normalize_settings(settings);n=Decimal(str(settings['divisor']))
    raw={k:number(inputs.get(k)) for k in ('revisedYield','revisedGrowth','pb','offensivePbScore','largeBankScore','nim','yearEndNim','provision','yearEndProvision')}
    scores={k:None for k in COMPONENTS}
    if raw['revisedYield'] is not None and raw['revisedGrowth'] is not None:scores['yield']=calc(lambda:raw['revisedYield']+raw['revisedGrowth']/n)
    if n==3 and inputs.get('offensivePbScore') is not None:scores['pb']=raw['offensivePbScore']
    elif raw['pb'] is not None and raw['pb']>0:scores['pb']=calc(lambda:1/raw['pb'])
    if raw['largeBankScore'] in (Decimal(0),Decimal('.5'),Decimal(1)):scores['large']=raw['largeBankScore']
    if raw['nim'] is not None and raw['yearEndNim'] is not None:scores['nim']=Decimal(1 if raw['nim']>raw['yearEndNim'] else -1 if raw['nim']<raw['yearEndNim'] else 0)
    if raw['provision'] is not None and raw['yearEndProvision'] is not None:scores['provision']=calc(lambda:min((raw['provision']-raw['yearEndProvision'])/10,Decimal(1)))
    weighted={};reasons={}
    for key,score in scores.items():
        weight=Decimal(str(settings['weights'][key]))
        weighted[key]=Decimal(0) if weight==0 else calc(lambda:score*weight/100) if score is not None else None
        if score is None:reasons[key]='缺少同口径核准输入或结果不可安全表示，不按0计分'
        elif weighted[key] is None:reasons[key]='加权结果不可安全表示'
    total=calc(lambda:sum(weighted.values(),Decimal(0))) if all(v is not None for v in weighted.values()) else None
    if total is None:reasons['total']='启用项缺少原始分/有效加权结果，总分未知'
    return dict(scores={k:float(v) if v is not None else None for k,v in scores.items()},
        weighted={k:float(v) if v is not None else None for k,v in weighted.items()},
        total=float(total) if total is not None else None,enabledCount=sum(v>0 for v in settings['weights'].values()),reasons=reasons)

def ah_discount(a_price,h_price,cny_per_hkd):
    """百分比；负值为H股折算成人民币后更便宜，原始三腿日期另需来源合同。"""
    a,h,fx=map(number,(a_price,h_price,cny_per_hkd))
    value=calc(lambda:(h*fx/a-1)*100) if all(v is not None and v>0 for v in (a,h,fx)) else None
    return float(value) if value is not None else None
