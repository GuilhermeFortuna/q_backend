from typing import Optional

from pydantic import BaseModel


class NewsArticleResponse(BaseModel):
    id: str
    title: str
    source: str
    publishedAt: str
    summary: str
    content: str
    videoUrl: Optional[str] = None
    imageUrl: Optional[str] = None
