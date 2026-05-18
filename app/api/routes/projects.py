from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.db.models import Project
from app.schemas.project import ProjectDetailOut

router = APIRouter(prefix="/v1/projects", tags=["projects"])


@router.get("/{project_id}", response_model=ProjectDetailOut)
async def get_project(project_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return project


@router.get("", response_model=list[ProjectDetailOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    published_only: bool = Query(True),
):
    query = select(Project).order_by(Project.id.desc()).limit(limit).offset(offset)
    if published_only:
        query = query.where(Project.is_published == True)
    result = await db.execute(query)
    return result.scalars().all()
