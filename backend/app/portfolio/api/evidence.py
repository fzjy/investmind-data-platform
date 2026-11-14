"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.portfolio.evidence import _get as read_manifest_cursor, _signatures as core_signatures, validate_manifest
__all__ = ['read_manifest_cursor', 'core_signatures', 'validate_manifest']
