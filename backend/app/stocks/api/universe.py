"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.stocks.universes import UniverseResolver
from app.stocks.universes.models import UniverseSelection
__all__ = ['UniverseResolver', 'UniverseSelection']
