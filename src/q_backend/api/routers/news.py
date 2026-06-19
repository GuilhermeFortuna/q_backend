from fastapi import APIRouter, HTTPException

from q_backend.api.schemas.news import NewsArticleResponse
from q_backend.api.services import news as news_service

router = APIRouter(tags=["news"])


@router.get("/api/v1/news", response_model=list[NewsArticleResponse])
def get_news_articles():
    return news_service.latest_articles(limit=25)


@router.get("/api/v1/news/{article_id}", response_model=NewsArticleResponse)
def get_news_article(article_id: str):
    try:
        return news_service.get_article(article_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
