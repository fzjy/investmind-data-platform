"""证券公开字段集合；构建器与发布校验复用唯一合同。"""

LIST_RISK_KEYS = ('highCashHighDebt', 'goodwill', 'dividendVsFinancing', 'realMoney', 'netCash')

SCREENER_FIELDS = {
    'listRisks', 'listMissingReasons',
    "researchValues", "financialTrends", "listHistory", "profitTrend", "payoutRatios", "metricBasis",
    "symbol", "name", "price", "change", "marketCap", "pe", "pb", "dividendYield",
    "roe", "grossMargin", "netMargin", "revenueGrowth", "profitGrowth", "realMoneyIndex",
    "cashflowGrowth", "dividendYears", "payoutRatio", "incomeSafety", "industry", "board",
    "isST", "isLoss", "listingYears", "pePercentile", "pbPercentile", "expectedDividendYield",
    "capitalReturnRatio", "roic", "institutionHolding", "majorIndustry", "subIndustry",
    "concepts", "region", "inventoryRisk", "netDebt",
}
