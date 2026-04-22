import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from deploy_app.config import DB_ROOT
from deploy_app.db import get_session
from deploy_app.deps import require_auth
from deploy_app.models import DatabaseInstance, Deployment, User, UserRole
from deploy_app.schemas import DatabaseCreateRequest, DatabaseRead
from deploy_app.services.deployments import (
    allocate_db_port,
    can_access_deployment,
    get_deployment_by_owner_repo,
    get_docker_config_dir_for_user,
    validate_owner_repo,
)
from deploy_app.services.docker_ops import (
    docker_compose_apply,
    docker_compose_down,
    render_db_compose,
)

router = APIRouter(prefix="/databases", tags=["databases"])


def build_owner_dir_name(owner_username: str) -> str:
	return re.sub(r"[^a-z0-9_-]", "-", owner_username.lower())


def get_database_or_404(session: Session, database_id: int) -> DatabaseInstance:
	db_instance = session.get(DatabaseInstance, database_id)
	if not db_instance:
		raise HTTPException(status_code=404, detail="База данных не найдена")
	return db_instance


def can_access_database(current_user: User, db_instance: DatabaseInstance) -> bool:
	return current_user.role == UserRole.ADMIN or db_instance.owner_id == current_user.id


@router.post("", response_model=DatabaseRead)
def create_database(
    body: DatabaseCreateRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> DatabaseRead:
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", body.name.strip()).strip("-")
    if len(safe_name) < 3:
        raise HTTPException(status_code=422, detail="Некорректное имя базы")

    deployment = None
    if body.deployment_repo is not None:
        validate_owner_repo(body.deployment_repo)
        deployment = get_deployment_by_owner_repo(session, body.deployment_repo)
        if not deployment:
            raise HTTPException(status_code=404, detail="Деплой не найден")
        if not can_access_deployment(current_user, deployment):
            raise HTTPException(status_code=403, detail="Нет доступа к деплою")

    duplicate = session.exec(
        select(DatabaseInstance).where(
            DatabaseInstance.owner_id == current_user.id,
            DatabaseInstance.name == safe_name,
        )
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="База с таким именем уже существует")

    owner_name = build_owner_dir_name(current_user.username)
    host_port = allocate_db_port(session, current_user)
    service_name = f"db-{owner_name}-{safe_name.lower()}"
    db_dir = DB_ROOT / owner_name / safe_name.lower()
    db_dir.mkdir(parents=True, exist_ok=True)
    volume_path = db_dir / "postgres"
    volume_path.mkdir(parents=True, exist_ok=True)

    compose_path = db_dir / "docker-compose.yml"
    compose_path.write_text(
        render_db_compose(
            service_name=service_name,
            volume_path=volume_path,
            host_port=host_port,
            postgres_image=body.postgres_image,
            postgres_user=body.postgres_user,
            postgres_password=body.postgres_password,
            postgres_db=body.postgres_db,
        ),
        encoding="utf-8",
    )

    db_instance = DatabaseInstance(
        owner_id=current_user.id or 0,
        deployment_id=deployment.id if deployment else None,
        name=safe_name,
        service_name=service_name,
        host_port=host_port,
        compose_path=str(compose_path),
        status="created",
    )
    session.add(db_instance)
    session.commit()
    session.refresh(db_instance)

    if body.run_deploy:
        try:
            docker_compose_apply(
                compose_path,
                docker_config_dir=get_docker_config_dir_for_user(current_user),
            )
            db_instance.status = "running"
            session.add(db_instance)
            session.commit()
            session.refresh(db_instance)
        except Exception as exc:
            db_instance.status = f"error: {exc}"
            session.add(db_instance)
            session.commit()
            raise HTTPException(status_code=500, detail=f"Не удалось поднять БД: {exc}") from exc

    return DatabaseRead(
        id=db_instance.id or 0,
        owner_id=db_instance.owner_id,
        deployment_repo=deployment.owner_repo if deployment else None,
        name=db_instance.name,
        service_name=db_instance.service_name,
        host_port=db_instance.host_port,
        compose_path=db_instance.compose_path,
        status=db_instance.status,
        created_at=db_instance.created_at,
    )


@router.get("", response_model=list[DatabaseRead])
def list_databases(
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
    deployment_repo: str | None = Query(default=None),
) -> list[DatabaseRead]:
    query = select(DatabaseInstance).order_by(col(DatabaseInstance.id))
    if current_user.role != UserRole.ADMIN:
        query = query.where(DatabaseInstance.owner_id == current_user.id)

    deployment = None
    if deployment_repo is not None:
        validate_owner_repo(deployment_repo)
        deployment = get_deployment_by_owner_repo(session, deployment_repo)
        if not deployment:
            return []
        if current_user.role != UserRole.ADMIN and deployment.owner_id != current_user.id:
            raise HTTPException(status_code=403, detail="Нет доступа к деплою")
        query = query.where(DatabaseInstance.deployment_id == deployment.id)

    dbs = session.exec(query).all()
    linked_ids = {db.deployment_id for db in dbs if db.deployment_id is not None}
    linked_deployments = {}
    if linked_ids:
        linked_deployments = {
            dep.id: dep
            for dep in session.exec(
                select(Deployment).where(Deployment.id.in_(linked_ids))
            ).all()
            if dep.id is not None
        }

    return [
        DatabaseRead(
            id=db.id or 0,
            owner_id=db.owner_id,
            deployment_repo=(
                linked_deployments[db.deployment_id].owner_repo
                if db.deployment_id in linked_deployments
                else None
            ),
            name=db.name,
            service_name=db.service_name,
            host_port=db.host_port,
            compose_path=db.compose_path,
            status=db.status,
            created_at=db.created_at,
        )
        for db in dbs
    ]


@router.post("/{database_id}/apply")
def apply_database(
    database_id: int,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    db_instance = get_database_or_404(session, database_id)
    if not can_access_database(current_user, db_instance):
        raise HTTPException(status_code=403, detail="Нет доступа к базе данных")

    compose_path = Path(db_instance.compose_path)
    if not compose_path.exists():
        raise HTTPException(status_code=404, detail="docker-compose.yml не найден")

    try:
        owner = session.get(User, db_instance.owner_id)
        if not owner:
            raise HTTPException(status_code=500, detail="Владелец базы не найден")
        docker_compose_apply(
            compose_path,
            docker_config_dir=get_docker_config_dir_for_user(owner),
        )
        db_instance.status = "running"
        session.add(db_instance)
        session.commit()
    except Exception as exc:
        db_instance.status = f"error: {exc}"
        session.add(db_instance)
        session.commit()
        raise HTTPException(status_code=500, detail=f"Не удалось запустить БД: {exc}") from exc

    return {"status": "ok"}


@router.delete("/{database_id}")
def delete_database(
    database_id: int,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    db_instance = get_database_or_404(session, database_id)
    if not can_access_database(current_user, db_instance):
        raise HTTPException(status_code=403, detail="Нет доступа к базе данных")

    compose_path = Path(db_instance.compose_path)
    if compose_path.exists():
        try:
            owner = session.get(User, db_instance.owner_id)
            if not owner:
                raise HTTPException(status_code=500, detail="Владелец базы не найден")
            docker_compose_down(
                compose_path,
                remove_volumes=True,
                docker_config_dir=get_docker_config_dir_for_user(owner),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Не удалось удалить БД контейнер: {exc}") from exc

    db_dir = compose_path.parent
    if db_dir.exists():
        shutil.rmtree(db_dir, ignore_errors=True)

    session.delete(db_instance)
    session.commit()
    return {"status": "ok"}
