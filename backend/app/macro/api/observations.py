"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.macro.dataset_query import select_visible_observations, stored_observations
__all__ = ['select_visible_observations', 'stored_observations']
