"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.portfolio.reference_catalog import capture_catalog
__all__ = ['capture_catalog']
