from datetime import datetime, date
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict

class DeveloperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    logo_s3_url: Optional[str] = None
    description: Optional[str] = None
    website: Optional[str] = None


class ProjectUnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    unit_type: Optional[str] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    price: Optional[float] = None
    price_from: Optional[float] = None
    price_to: Optional[float] = None
    size: Optional[float] = None
    currency: Optional[str] = None
    area_unit: Optional[str] = None
    layout_name: Optional[str] = None
    status: Optional[str] = None


class ProjectAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    s3_url: Optional[str] = None
    filename: Optional[str] = None
    position: int


class ProjectDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    district: Optional[str] = None
    country: Optional[str] = None
    sale_status: Optional[str] = None
    status: Optional[str] = None
    completion_quarter: Optional[str] = None
    completion_date: Optional[date] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    currency: Optional[str] = None
    description: Optional[str] = None
    short_description: Optional[str] = None
    cover_image_url: Optional[str] = None
    marketing_brochure_url: Optional[str] = None
    amenities: Optional[Any] = None
    units_count: Optional[int] = None
    is_published: bool

    developer: Optional[DeveloperOut] = None
    units: list[ProjectUnitOut] = []
    assets: list[ProjectAssetOut] = []
