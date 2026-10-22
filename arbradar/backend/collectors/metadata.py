"""Stable bookmaker grouping for the feed diagnostics shown in ArbRadar."""

FAMILY_MEMBERS = {
    "1xBet LineFeed": {"1xbet", "22bet", "helabet", "betwinner", "megapari", "1xbit", "melbet"},
    "BetConstruct WebSocket": {"meridianbet", "pigabet"},
    "Betika public API": {"betika"},
    "LeonBet public API": {"leonbet"},
    "PremierBet Sahara feed": {"premierbet"},
    "SportPesa public API": {"sportpesa"},
    "BetPawa sportsbook feed": {"betpawa"},
    "SportyBet public API": {"sportybet"},
    "BangBet sportsbook API": {"bangbet"},
    "ThroneBet Graph API": {"thronebet"},
    "WasafiBet sportsbook feed": {"wasafibet"},
}


def bookmaker_family(bookmaker: str) -> str:
    for family, members in FAMILY_MEMBERS.items():
        if bookmaker.lower() in members:
            return family
    return "Other / unclassified feed"


def group_bookmakers(bookmakers: list[str]) -> list[dict]:
    grouped = {}
    for bookmaker in sorted(set(bookmakers)):
        grouped.setdefault(bookmaker_family(bookmaker), []).append(bookmaker)
    return [{"name": family, "bookmakers": names} for family, names in grouped.items()]
