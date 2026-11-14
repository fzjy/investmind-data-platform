from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


def controlled_catalog() -> MacroSeriesCatalog:
    """Explicit test-only sources exercise DTO logic, not live provider validity."""
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    template = catalog.require("cn.cpi.yoy").sources[0]
    return catalog.model_copy(update={"series": tuple(
        item if item.sources else item.model_copy(update={"sources": (
            template.model_copy(update={"provider": "fixture", "priority": 1}),
            template.model_copy(update={"provider": "tushare", "priority": 2}),
        )}) for item in catalog.series
    )})
