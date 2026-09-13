"""配方标签推导 API。

提供原料营养/过敏原资料、嵌套配方、单位换算、标签规则的版本化管理，
递归展开成分树并按份量推导营养标签与过敏原来源路径。
"""

from .app_factory import create_app

__all__ = ["create_app"]
