"""在同一只读发布视图输出网站转换器的标准 JSON 输入。由调用方通过管道交付给 Node。"""
from argparse import ArgumentParser
import json
from pathlib import Path
import sys

from sqlalchemy import text
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.shared.readonly_database import open_readonly_factory


def sample(view):
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        if view == "macro-cards":
            version, enterprise = session.execute(text("""
                select version, payload->'enterpriseAnalysis' from mart.published_snapshot where product='macro-transmission'
            """)).one()
            return {"meta":{"version":version},"data":{"enterpriseAnalysis":enterprise}}
        if view == "statements":
            symbols = ["000001.SZ","600519.SH","920002.BJ"]
            rows = session.execute(text("""
                select payload->>'symbol' as symbol, version, payload->'financial'->'statements' as statements
                from mart.published_snapshot where product=any(:products) order by product
            """),{"products":["stock-workbench:"+s for s in symbols]}).mappings().all()
            assert [row["symbol"] for row in rows] == symbols
        else:
            rows = session.execute(text("""
                select payload->>'symbol' as symbol,version,payload->'quote' as quote,
                       payload->'metrics' as metrics,payload->'financial'->'statements'->'tables' as tables
                from mart.published_snapshot where product like 'stock-workbench:%' order by product
            """)).mappings().all()
            assert rows, "工作台未发布"
        return list(map(dict,rows))


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("view", choices=("macro-cards", "statements", "workbenches"))
    print(json.dumps(sample(parser.parse_args().view),ensure_ascii=True))
