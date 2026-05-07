from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ResumeProfile(BaseModel):
    name: Optional[str] = None
    education: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    projects: List[str] = Field(default_factory=list)
    experience: List[str] = Field(default_factory=list)
    courses: List[str] = Field(default_factory=list)
    target_roles: List[str] = Field(default_factory=list)
    search_keywords: List[str] = Field(default_factory=list)


class KeywordEntry(BaseModel):
    id: Optional[int] = None
    keyword: str
    category: str = "general"
    priority: int = 3
    enabled: bool = True
    source: str = "ai_generated"
    created_at: Optional[datetime] = None


class LocationPreference(BaseModel):
    id: Optional[int] = None
    city: str
    province_state: str = ""
    country: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_km: int = 25
    work_mode: str = "hybrid"
    created_at: Optional[datetime] = None


class JobMatch(BaseModel):
    id: Optional[int] = None
    job_apply_link: str
    keyword_id: Optional[int] = None
    match_score: int
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    ai_reason: str = ""
    status: str = "new"
    created_at: Optional[datetime] = None
