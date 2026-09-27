"""
LLM Financial Advisor — prompt generation and context management.

This module generates prompts for LLM-based financial advice and manages
the growing context file that makes future advice more personalized.

Usage:
    ./run.sh advisor              # Generate prompt for current month
    ./run.sh advisor --month 2026-02
    ./run.sh advisor ingest       # Ingest LLM response into context
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .constants import DEFAULT_DB, DEFAULT_ADVISOR_DIR
from .db import INCOME_SQL, NOT_SAVINGS_SQL, REFUND_SQL, SPEND_SQL
from .pdf_report import previous_month_label, month_display_name, _prev_month


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_CONTEXT_FILE = DEFAULT_ADVISOR_DIR / "context.json"


def _ensure_advisor_dir() -> None:
    """Create the advisor directory if it doesn't exist."""
    DEFAULT_ADVISOR_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------

def load_context(context_path: Path = DEFAULT_CONTEXT_FILE) -> dict[str, Any]:
    """Load the advisor context file, or return empty context if none exists."""
    if not context_path.exists():
        return {
            "version": 1,
            "created_at": None,
            "last_updated": None,
            "profile": {},
            "goals": {
                "short_term": [],
                "medium_term": [],
                "long_term": [],
            },
            "insights": [],
            "monthly_summaries": {},
        }
    
    with context_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_context(
    context: dict[str, Any],
    context_path: Path = DEFAULT_CONTEXT_FILE,
) -> None:
    """Save the advisor context file."""
    _ensure_advisor_dir()
    context["last_updated"] = date.today().isoformat()
    if context["created_at"] is None:
        context["created_at"] = context["last_updated"]
    
    with context_path.open("w", encoding="utf-8") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)


def is_first_run(context: dict[str, Any]) -> bool:
    """Check if this is the first advisor run (no profile or goals set)."""
    profile = context.get("profile", {})
    goals = context.get("goals", {})
    
    has_profile = bool(profile.get("financial_situation") or profile.get("income_type"))
    has_goals = any(goals.get(k) for k in ["short_term", "medium_term", "long_term"])
    
    return not (has_profile or has_goals)


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def _get_months_in_range(start_month: str, end_month: str) -> list[str]:
    """Get list of months (YYYY-MM) from start to end inclusive."""
    months = []
    current = start_month
    while current <= end_month:
        months.append(current)
        # Move to next month
        year, month = map(int, current.split("-"))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        current = f"{year:04d}-{month:02d}"
    return months


def _fetch_monthly_summary(
    conn: sqlite3.Connection,
    month: str,
) -> dict[str, Any]:
    """Fetch summary statistics for a single month."""
    conn.row_factory = sqlite3.Row
    
    # Get totals
    row = conn.execute(f"""
        SELECT
            COUNT(*) as tx_count,
            SUM(CASE WHEN {INCOME_SQL} THEN amount_abs ELSE 0 END) as income,
            SUM({SPEND_SQL}) as expenses
        FROM transactions
        WHERE month = ?
    """, (month,)).fetchone()
    
    if not row or row["tx_count"] == 0:
        conn.row_factory = None
        return {}
    
    # Get category breakdown (expenses net of refunds)
    categories = conn.execute(f"""
        SELECT 
            COALESCE(category, 'Uncategorized') as category,
            SUM({SPEND_SQL}) as total,
            SUM(direction = 'out') as count
        FROM transactions
        WHERE month = ? AND (direction = 'out' OR {REFUND_SQL}) AND {NOT_SAVINGS_SQL}
        GROUP BY category
        ORDER BY total DESC
    """, (month,)).fetchall()
    
    # Get top merchants
    merchants = conn.execute(f"""
        SELECT 
            description_clean as merchant,
            SUM(amount_abs) as total,
            COUNT(*) as count
        FROM transactions
        WHERE month = ? AND direction = 'out' AND {NOT_SAVINGS_SQL}
        GROUP BY description_clean
        ORDER BY total DESC
        LIMIT 10
    """, (month,)).fetchall()
    
    conn.row_factory = None
    
    return {
        "month": month,
        "month_name": month_display_name(month),
        "transaction_count": row["tx_count"],
        "income": round(row["income"] or 0, 2),
        "expenses": round(row["expenses"] or 0, 2),
        "net": round((row["income"] or 0) - (row["expenses"] or 0), 2),
        "categories": [
            {"name": c["category"], "total": round(c["total"], 2), "count": c["count"]}
            for c in categories
        ],
        "top_merchants": [
            {"name": m["merchant"], "total": round(m["total"], 2), "count": m["count"]}
            for m in merchants
        ],
    }


def _fetch_historical_summary(
    conn: sqlite3.Connection,
    before_month: str,
) -> dict[str, Any]:
    """Fetch aggregated summary for all months before the given month."""
    conn.row_factory = sqlite3.Row
    
    # Get overall totals
    row = conn.execute(f"""
        SELECT
            COUNT(DISTINCT month) as month_count,
            COUNT(*) as tx_count,
            SUM(CASE WHEN {INCOME_SQL} THEN amount_abs ELSE 0 END) as total_income,
            SUM({SPEND_SQL}) as total_expenses,
            MIN(month) as first_month,
            MAX(month) as last_month
        FROM transactions
        WHERE month < ?
    """, (before_month,)).fetchone()
    
    if not row or row["month_count"] == 0:
        conn.row_factory = None
        return {}
    
    # Get average category spending (net of refunds)
    categories = conn.execute(f"""
        SELECT 
            COALESCE(category, 'Uncategorized') as category,
            SUM({SPEND_SQL}) as total,
            AVG(CASE WHEN direction = 'out' THEN amount_abs END) as avg_per_tx,
            SUM(direction = 'out') as count
        FROM transactions
        WHERE month < ? AND (direction = 'out' OR {REFUND_SQL}) AND {NOT_SAVINGS_SQL}
        GROUP BY category
        ORDER BY total DESC
    """, (before_month,)).fetchall()
    
    conn.row_factory = None
    
    month_count = row["month_count"]
    return {
        "period": f"{row['first_month']} to {row['last_month']}",
        "month_count": month_count,
        "transaction_count": row["tx_count"],
        "total_income": round(row["total_income"] or 0, 2),
        "total_expenses": round(row["total_expenses"] or 0, 2),
        "avg_monthly_income": round((row["total_income"] or 0) / month_count, 2),
        "avg_monthly_expenses": round((row["total_expenses"] or 0) / month_count, 2),
        "avg_monthly_net": round(
            ((row["total_income"] or 0) - (row["total_expenses"] or 0)) / month_count, 2
        ),
        "categories": [
            {
                "name": c["category"],
                "total": round(c["total"], 2),
                "monthly_avg": round(c["total"] / month_count, 2),
            }
            for c in categories[:15]  # Top 15 categories
        ],
    }


def fetch_expense_data(
    db_path: Path = DEFAULT_DB,
    target_month: str | None = None,
    detailed_months: int = 12,
) -> dict[str, Any]:
    """
    Fetch expense data for prompt generation.
    
    Args:
        db_path: Path to the SQLite database.
        target_month: The month to analyze (YYYY-MM). Defaults to previous month.
        detailed_months: Number of recent months to include with full detail.
    
    Returns:
        Dict with:
        - target_month: The month being analyzed
        - recent_months: List of detailed monthly summaries (last N months)
        - historical_summary: Aggregated data for older months
    """
    if target_month is None:
        target_month = previous_month_label()
    
    conn = sqlite3.connect(str(db_path))
    try:
        # Calculate the range for detailed months
        # Go back `detailed_months` from target_month
        year, month = map(int, target_month.split("-"))
        
        # Find start month for detailed data
        for _ in range(detailed_months - 1):
            if month == 1:
                year -= 1
                month = 12
            else:
                month -= 1
        start_month = f"{year:04d}-{month:02d}"
        
        # Fetch detailed data for recent months
        recent_months = []
        months_to_fetch = _get_months_in_range(start_month, target_month)
        for m in months_to_fetch:
            summary = _fetch_monthly_summary(conn, m)
            if summary:
                recent_months.append(summary)
        
        # Fetch historical summary for older data
        historical = _fetch_historical_summary(conn, start_month)
        
        return {
            "target_month": target_month,
            "target_month_name": month_display_name(target_month),
            "recent_months": recent_months,
            "historical_summary": historical,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompt generation
# ---------------------------------------------------------------------------

def _format_currency(amount: float) -> str:
    """Format amount as currency."""
    return f"€{amount:,.2f}"


def _format_monthly_data(month_data: dict[str, Any]) -> str:
    """Format a single month's data for the prompt."""
    lines = [
        f"### {month_data['month_name']} ({month_data['month']})",
        f"- **Income**: {_format_currency(month_data['income'])}",
        f"- **Expenses**: {_format_currency(month_data['expenses'])}",
        f"- **Net**: {_format_currency(month_data['net'])}",
        f"- **Transactions**: {month_data['transaction_count']}",
        "",
        "**Spending by Category:**",
    ]
    
    for cat in month_data.get("categories", [])[:10]:
        lines.append(f"- {cat['name']}: {_format_currency(cat['total'])} ({cat['count']} txns)")
    
    if month_data.get("top_merchants"):
        lines.extend(["", "**Top Merchants:**"])
        for m in month_data["top_merchants"][:5]:
            lines.append(f"- {m['name']}: {_format_currency(m['total'])} ({m['count']} txns)")
    
    return "\n".join(lines)


def _format_historical_data(historical: dict[str, Any]) -> str:
    """Format historical summary for the prompt."""
    if not historical:
        return "No historical data available (this appears to be the first year of tracking)."
    
    lines = [
        f"### Historical Summary ({historical['period']})",
        f"- **Months tracked**: {historical['month_count']}",
        f"- **Average monthly income**: {_format_currency(historical['avg_monthly_income'])}",
        f"- **Average monthly expenses**: {_format_currency(historical['avg_monthly_expenses'])}",
        f"- **Average monthly net**: {_format_currency(historical['avg_monthly_net'])}",
        "",
        "**Average Monthly Spending by Category:**",
    ]
    
    for cat in historical.get("categories", []):
        lines.append(f"- {cat['name']}: {_format_currency(cat['monthly_avg'])}/month")
    
    return "\n".join(lines)


def _format_context(context: dict[str, Any]) -> str:
    """Format the user context for the prompt."""
    if is_first_run(context):
        return "No prior context available — this is the first advisor session."
    
    lines = ["## User Context (from previous sessions)"]
    
    profile = context.get("profile", {})
    if profile:
        lines.append("\n### Profile")
        for key, value in profile.items():
            if value:
                nice_key = key.replace("_", " ").title()
                lines.append(f"- **{nice_key}**: {value}")
    
    goals = context.get("goals", {})
    if any(goals.values()):
        lines.append("\n### Financial Goals")
        for term, goal_list in goals.items():
            if goal_list:
                nice_term = term.replace("_", " ").title()
                lines.append(f"\n**{nice_term}:**")
                for goal in goal_list:
                    lines.append(f"- {goal}")
    
    insights = context.get("insights", [])
    if insights:
        lines.append("\n### Key Insights from Previous Sessions")
        for insight in insights[-5:]:  # Last 5 insights
            lines.append(f"- {insight}")
    
    return "\n".join(lines)


def generate_prompt(
    db_path: Path = DEFAULT_DB,
    context_path: Path = DEFAULT_CONTEXT_FILE,
    target_month: str | None = None,
) -> str:
    """
    Generate the LLM prompt for financial advice.
    
    Args:
        db_path: Path to the SQLite database.
        context_path: Path to the context JSON file.
        target_month: The month to analyze (YYYY-MM). Defaults to previous month.
    
    Returns:
        The complete prompt as a string.
    """
    context = load_context(context_path)
    data = fetch_expense_data(db_path, target_month)
    first_run = is_first_run(context)
    
    # Build the prompt
    sections = []
    
    # System instructions
    sections.append("""# Financial Advisor Analysis Request

You are a personal financial advisor analyzing expense data. Your role is to:
1. Analyze spending patterns and trends
2. Identify areas for potential savings
3. Track progress toward financial goals
4. Provide actionable, specific advice
5. Be encouraging but honest about areas needing improvement

Please provide your analysis in a structured format that can be saved and referenced later.""")
    
    # User context
    sections.append("")
    sections.append(_format_context(context))
    
    # Current month data
    sections.append("")
    sections.append("## Current Month Analysis")
    sections.append(f"**Analyzing: {data['target_month_name']}**")
    
    # Find target month in recent_months
    target_data = None
    for m in data["recent_months"]:
        if m["month"] == data["target_month"]:
            target_data = m
            break
    
    if target_data:
        sections.append("")
        sections.append(_format_monthly_data(target_data))
    else:
        sections.append(f"\nNo data found for {data['target_month_name']}.")
    
    # Recent months (excluding target)
    other_months = [m for m in data["recent_months"] if m["month"] != data["target_month"]]
    if other_months:
        sections.append("")
        sections.append("## Recent Months (for comparison)")
        for month_data in reversed(other_months):  # Oldest first
            sections.append("")
            sections.append(_format_monthly_data(month_data))
    
    # Historical summary
    if data.get("historical_summary"):
        sections.append("")
        sections.append("## Historical Data (older than 12 months)")
        sections.append(_format_historical_data(data["historical_summary"]))
    
    # Request section
    sections.append("")
    sections.append("---")
    sections.append("")
    
    if first_run:
        sections.append("""## First Session — Discovery Questions

Since this is our first session, please:

1. **Analyze the data** provided above and share your initial observations
2. **Ask me questions** to understand my financial situation better:
   - What is my employment/income situation?
   - What are my financial priorities right now?
   - Do I have any specific savings goals?
   - Are there any major expenses coming up?
   - What's my general approach to money (saver vs spender)?

3. **Provide initial recommendations** based on what you can see in the data

Please structure your response with clear sections:
- **Initial Observations** (what stands out in the data)
- **Questions for You** (to build context for future sessions)
- **Preliminary Recommendations** (actionable items based on current data)

After I answer your questions, we can establish goals and track progress in future sessions.""")
    else:
        sections.append("""## Monthly Review Request

Please provide:

1. **Month Summary**: Key observations about this month's spending
2. **Trend Analysis**: How does this month compare to recent months and averages?
3. **Goal Progress**: Am I on track with my stated goals?
4. **Concerns**: Any spending patterns that need attention?
5. **Recommendations**: 2-3 specific, actionable suggestions for next month
6. **Positive Notes**: What am I doing well?

If any goals need updating or if you have questions to refine your advice, please ask.

Structure your response with clear headers so it can be easily referenced later.""")
    
    return "\n".join(sections)


def save_prompt(
    prompt: str,
    target_month: str | None = None,
    output_dir: Path = DEFAULT_ADVISOR_DIR,
) -> Path:
    """Save the generated prompt to a file."""
    _ensure_advisor_dir()
    
    if target_month is None:
        target_month = previous_month_label()
    
    output_path = output_dir / f"prompt-{target_month}.md"
    output_path.write_text(prompt, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Response ingestion
# ---------------------------------------------------------------------------

def get_response_path(
    target_month: str | None = None,
    advisor_dir: Path = DEFAULT_ADVISOR_DIR,
) -> Path:
    """Get the expected path for a response file."""
    if target_month is None:
        target_month = previous_month_label()
    return advisor_dir / f"response-{target_month}.md"


def find_months_without_responses(
    db_path: Path = DEFAULT_DB,
    advisor_dir: Path = DEFAULT_ADVISOR_DIR,
) -> list[str]:
    """
    Find all months with transaction data but no advisor response.
    
    Returns a list of months (YYYY-MM) sorted oldest first.
    """
    import sqlite3
    
    # Get all months with transactions
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("""
            SELECT DISTINCT month FROM transactions
            WHERE month <= ?
            ORDER BY month ASC
        """, (previous_month_label(),)).fetchall()
        all_months = [r[0] for r in rows]
    finally:
        conn.close()
    
    # Filter out months that already have responses
    missing = []
    for month in all_months:
        response_path = advisor_dir / f"response-{month}.md"
        if not response_path.exists():
            missing.append(month)
    
    return missing


def find_next_catchup_month(
    db_path: Path = DEFAULT_DB,
    advisor_dir: Path = DEFAULT_ADVISOR_DIR,
) -> str | None:
    """
    Find the next month that needs advisor attention.
    
    Returns the oldest month with transaction data but no response,
    or None if all months are covered.
    """
    missing = find_months_without_responses(db_path, advisor_dir)
    return missing[0] if missing else None


def ingest_response(
    response_path: Path,
    context_path: Path = DEFAULT_CONTEXT_FILE,
    target_month: str | None = None,
) -> dict[str, Any]:
    """
    Ingest an LLM response and update the context file.
    
    This is a simple ingestion that stores the response reference.
    The user can manually update goals/insights in the context file,
    or we can add interactive prompts later.
    
    Args:
        response_path: Path to the response markdown file.
        context_path: Path to the context JSON file.
        target_month: The month this response is for.
    
    Returns:
        The updated context dict.
    """
    if target_month is None:
        target_month = previous_month_label()
    
    if not response_path.exists():
        raise FileNotFoundError(f"Response file not found: {response_path}")
    
    context = load_context(context_path)
    
    # Record that we have a response for this month
    if "responses" not in context:
        context["responses"] = {}
    
    context["responses"][target_month] = {
        "file": str(response_path),
        "ingested_at": date.today().isoformat(),
    }
    
    save_context(context, context_path)
    return context


def update_context_interactive(
    context_path: Path = DEFAULT_CONTEXT_FILE,
) -> dict[str, Any]:
    """
    Interactive prompt to update context after reading LLM response.
    
    This allows the user to add goals and insights from the terminal.
    """
    context = load_context(context_path)
    
    print("\n" + "=" * 60)
    print("Update your financial context")
    print("=" * 60)
    print("\nAfter reviewing the advisor's response, you can update your context.")
    print("Press Enter to skip any section.\n")
    
    # Profile updates
    print("--- Profile ---")
    for field in ["financial_situation", "income_type", "risk_tolerance", "savings_priority"]:
        current = context.get("profile", {}).get(field, "")
        prompt_text = f"{field.replace('_', ' ').title()}"
        if current:
            prompt_text += f" (current: {current})"
        prompt_text += ": "
        
        value = input(prompt_text).strip()
        if value:
            if "profile" not in context:
                context["profile"] = {}
            context["profile"][field] = value
    
    # Goals
    print("\n--- Goals (enter one per line, empty line to finish) ---")
    for term in ["short_term", "medium_term", "long_term"]:
        print(f"\n{term.replace('_', ' ').title()} goals:")
        current_goals = context.get("goals", {}).get(term, [])
        if current_goals:
            print(f"  Current: {', '.join(current_goals)}")
        
        new_goals = []
        while True:
            goal = input("  Add goal: ").strip()
            if not goal:
                break
            new_goals.append(goal)
        
        if new_goals:
            if "goals" not in context:
                context["goals"] = {"short_term": [], "medium_term": [], "long_term": []}
            context["goals"][term].extend(new_goals)
    
    # Insights
    print("\n--- Key Insights (enter one per line, empty line to finish) ---")
    print("Add any important insights from the advisor's response:")
    while True:
        insight = input("  Insight: ").strip()
        if not insight:
            break
        if "insights" not in context:
            context["insights"] = []
        context["insights"].append(insight)
    
    save_context(context, context_path)
    print("\nContext updated!")
    return context
