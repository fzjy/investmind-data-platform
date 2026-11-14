"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.stocks.jobs.daily_sync_repository import DailySyncRepository
__all__ = ['DailySyncRepository']
