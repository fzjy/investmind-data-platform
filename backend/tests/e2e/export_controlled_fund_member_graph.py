"""受控17家/5交集的实际三收据图，临时 PostgreSQL，不连接服务。"""
from datetime import date,timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.tests.support.postgres import PostgresContainer, standalone_factory
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
from app.macro.enterprise_official_targets import build_official_target_comparison,load_official_witnesses,load_official_source_states
from backend.tests.macro.test_enterprise_rea import controlled
from backend.tests.macro.test_fund_member_graph_store import setup,store
from backend.tests.macro.test_fund_member_graph import NOW
from backend.tests.macro.official_xls_fixture import compound_workbook
from backend.tests.macro.test_official_members import row
from backend.tests.macro.test_official_pcf import body as pcf_body


def cases():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as instance, TemporaryDirectory() as directory:
        path = Path(directory)
        with standalone_factory(instance.get_connection_url()) as factory:
            c,p=setup(factory,path);s=store(factory);data=controlled(17);members=list(data.symbols[3:8])
            key=s.reserve(p,'csi-members',NOW+timedelta(seconds=1))
            raw=compound_workbook(rows=[row(index='399967',date='20261022',code=symbol[:6],exchange='深圳证券交易所',exchangeEnglish='Shenzhen Stock Exchange') for symbol in members])
            s.receive(key,raw,NOW+timedelta(seconds=2));s.replay(key,NOW+timedelta(seconds=3))
            key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4))
            components=''.join(f'<Component><InstrumentID>{symbol[:6]}</InstrumentID><Quantity>0</Quantity><SubstitutionFlag>2</SubstitutionFlag></Component>' for symbol in members)
            s.receive(key,pcf_body(code='512660',day='20261022',records=str(len(members)),components=components),NOW+timedelta(seconds=5));s.replay(key,NOW+timedelta(seconds=6))
            at=NOW+timedelta(seconds=7);policy=EnterprisePolicy(min_samples=1);parent=public_enterprise_analysis(data,date(2026,10,26),policy,rea_enabled=True)
            w=load_official_witnesses(factory,at,c);states=load_official_source_states(factory,at,c,w)
            parent['officialTargets']=build_official_target_comparison(data,date(2026,10,26),policy,parent,c,w,states)
            assert valid_enterprise_analysis(parent)
            return parent



if __name__=='__main__':print(json.dumps(cases(),allow_nan=False))
