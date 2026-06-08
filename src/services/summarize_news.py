"""
Service: summarize_news

Calls Gemini with full article context when available; returns summary plus
referenced_articles and citation_placements for source linking in the memo.
"""

from __future__ import annotations
from datetime import datetime

from src.models.news import NewsItem, NewsSummary, ReferencedArticle
from src.models.step_result import Status, StepResult, StepTimer

STEP = "summarize_news"


def _article_dicts(items: list[NewsItem]) -> list[dict]:
    out = []
    for i, n in enumerate(items):
        dt = n.published_at
        date_str = dt.strftime("%b %d") if isinstance(dt, datetime) else (str(dt)[:10] if dt else "")
        out.append({
            "index": i,
            "headline": n.headline or "",
            "source": n.source or "",
            "date": date_str,
            "url": n.url or "",
            "snippet": (n.snippet or "")[:300],
        })
    return out


def run(news_items: list[NewsItem], company_name: str, memo_fact_pack: dict | None = None) -> StepResult:
    with StepTimer() as t:
        if not news_items and not memo_fact_pack:
            return StepResult(
                step_name=STEP, status=Status.SKIPPED, source="gemini",
                message="No news items and no memo fact pack",
                elapsed_seconds=t.elapsed,
            )
        try:
            from src.providers.gemini import summarize_news as gemini_summarize
            articles = _article_dicts(news_items) if news_items else None
            raw = gemini_summarize(
                company_name=company_name,
                articles=articles,
                memo_fact_pack=memo_fact_pack,
            )

            ref_indices = raw.get("referenced_article_indices") or []
            ref_articles: list[ReferencedArticle] = []
            for idx in ref_indices:
                if 0 <= idx < len(news_items):
                    n = news_items[idx]
                    ref_articles.append(ReferencedArticle(
                        source=n.source or "",
                        headline=n.headline or "",
                        published_at=n.published_at,
                        url=n.url or "",
                        extracted_fact="",
                    ))
            citation_placements = raw.get("citation_placements") or []
            if not isinstance(citation_placements, list):
                citation_placements = []
            # Normalize: Gemini sometimes returns list of lists e.g. [[279, 282]]; require list[dict]
            normalized_placements: list[dict] = []
            for p in citation_placements:
                if isinstance(p, dict):
                    normalized_placements.append(p)
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    normalized_placements.append({
                        "paragraph": int(p[0]) if len(p) > 0 else 1,
                        "after_sentence": int(p[1]) if len(p) > 1 else 0,
                        "article_index": int(p[2]) if len(p) > 2 else 0,
                    })
            citation_placements = normalized_placements

            p1 = raw.get("investment_view_paragraph_1", "") or ""
            p2 = raw.get("investment_view_paragraph_2", "") or ""
            # Apply guardrail once at source so payload and render both see clean IV
            from src.services.qa_engine import guardrail_paragraphs
            p1, p2 = guardrail_paragraphs(p1, p2)

            summary = NewsSummary(
                themes=raw.get("themes", []),
                overall_sentiment=raw.get("overall_sentiment", "neutral"),
                key_items=raw.get("key_items", []),
                uncertainty_factors=raw.get("uncertainty_factors", []),
                summary_text=raw.get("summary_text", ""),
                investment_view_bullets=raw.get("investment_view_bullets", []),
                investment_view_paragraph_1=p1,
                investment_view_paragraph_2=p2,
                referenced_articles=ref_articles,
                citation_placements=citation_placements,
            )
            return StepResult(
                step_name=STEP, status=Status.SUCCESS, source="gemini",
                message=(
                    f"Summary: {len(summary.themes)} themes, "
                    f"sentiment={summary.overall_sentiment}, "
                    f"refs={len(ref_articles)}"
                ),
                data=summary, elapsed_seconds=t.elapsed,
            )
        except Exception as exc:
            return StepResult(
                step_name=STEP, status=Status.FAILED, source="gemini",
                message="Gemini summarization failed",
                error_detail=str(exc),
                elapsed_seconds=t.elapsed,
            )
