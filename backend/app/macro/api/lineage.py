"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.macro.import_lineage import verify_import_lineage
__all__ = ['verify_import_lineage']
