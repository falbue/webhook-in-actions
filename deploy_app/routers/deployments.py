from datetime import datetime
from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, col, select

from deploy_app.config import DEPLOY_ROOT
from deploy_app.db import get_session
from deploy_app.deps import require_auth
from deploy_app.models import DatabaseInstance, Deployment, User, UserRole
from deploy_app.schemas import (
    DeploymentCreateRequest,
    DeploymentRead,
    DeploymentRedeployRequest,
    EnvPatchRequest,
    EnvReplaceRequest,
)
from deploy_app.services.deployments import (
    allocate_app_port,
    can_access_deployment,
    check_deploy_limit,
    build_owner_slug,
    build_project_name,
    build_repo_slug,
    dump_env,
    get_deployment_by_owner_repo,
    get_docker_config_dir_for_user,
    parse_env,
    validate_owner_repo,
    write_env_file,
)
from deploy_app.services.docker_ops import (
    docker_compose_apply,
    docker_compose_down,
    render_app_compose,
)

router = APIRouter(prefix="/deployments", tags=["deployments"])


def get_deployment_for_repo(
    session: Session, owner_repo: str, current_user: User
) -> Deployment:
    deployment = get_deployment_by_owner_repo(session, owner_repo)
    if not deployment:
        raise HTTPException(status_code=404, detail="Деплой не найден")
    if not can_access_deployment(current_user, deployment):
        raise HTTPException(status_code=403, detail="Нет доступа к деплою")
    return deployment


@router.post("", response_model=DeploymentRead)
def create_deployment(
    body: DeploymentCreateRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> DeploymentRead:
    validate_owner_repo(body.owner_repo)
    check_deploy_limit(session, current_user)

    existing = session.exec(
        select(Deployment).where(Deployment.owner_repo == body.owner_repo)
    ).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="Этот репозиторий уже задеплоен"
        )

    owner_name = build_owner_slug(current_user.username)
    repo_dir_name = build_repo_slug(body.owner_repo)
    project_name = build_project_name(body.owner_repo, current_user.username)
    app_port = allocate_app_port(session, current_user)
    deploy_path = DEPLOY_ROOT / owner_name / repo_dir_name
    deploy_path.mkdir(parents=True, exist_ok=True)

    compose_path = deploy_path / "docker-compose.yml"
    compose_path.write_text(
        render_app_compose(project_name, body.owner_repo, body.tag, app_port),
        encoding="utf-8",
    )

    env_path = deploy_path / ".env"
    if not env_path.exists():
        write_env_file(env_path, "")

    deployment = Deployment(
        owner_id=current_user.id or 0,
        owner_repo=body.owner_repo,
        tag=body.tag,
        app_port=app_port,
        deploy_path=str(deploy_path),
        updated_at=datetime.utcnow(),
    )
    session.add(deployment)
    session.commit()
    session.refresh(deployment)

    if body.run_deploy:
        try:
            docker_compose_apply(
                compose_path,
                docker_config_dir=get_docker_config_dir_for_user(current_user),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Не удалось выполнить deploy: {exc}"
            ) from exc

    return DeploymentRead(
        id=deployment.id or 0,
        owner_id=deployment.owner_id,
        owner_repo=deployment.owner_repo,
        tag=deployment.tag,
        app_port=deployment.app_port,
        deploy_path=deployment.deploy_path,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
    )


@router.get("", response_model=list[DeploymentRead])
def list_deployments(
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> list[DeploymentRead]:
    query = select(Deployment).order_by(col(Deployment.id))
    if current_user.role != UserRole.ADMIN:
        query = query.where(Deployment.owner_id == current_user.id)

    deployments = session.exec(query).all()
    return [
        DeploymentRead(
            id=deployment.id or 0,
            owner_id=deployment.owner_id,
            owner_repo=deployment.owner_repo,
            tag=deployment.tag,
            app_port=deployment.app_port,
            deploy_path=deployment.deploy_path,
            created_at=deployment.created_at,
            updated_at=deployment.updated_at,
        )
        for deployment in deployments
    ]


@router.post("/{owner_repo:path}/redeploy", response_model=DeploymentRead)
def redeploy(
    owner_repo: str,
    body: DeploymentRedeployRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> DeploymentRead:
    validate_owner_repo(owner_repo)
    deployment = get_deployment_for_repo(session, owner_repo, current_user)

    deployment.tag = body.tag
    deployment.updated_at = datetime.utcnow()
    deploy_path = Path(deployment.deploy_path)
    owner = session.get(User, deployment.owner_id)
    if not owner:
        raise HTTPException(status_code=500, detail="Владелец деплоя не найден")
    project_name = build_project_name(deployment.owner_repo, owner.username)
    compose_path = deploy_path / "docker-compose.yml"
    compose_path.write_text(
        render_app_compose(
            project_name,
            deployment.owner_repo,
            deployment.tag,
            deployment.app_port,
        ),
        encoding="utf-8",
    )
    session.add(deployment)
    session.commit()
    session.refresh(deployment)

    try:
        docker_compose_apply(
            compose_path,
            docker_config_dir=get_docker_config_dir_for_user(owner),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Не удалось выполнить redeploy: {exc}"
        ) from exc

    return DeploymentRead(
        id=deployment.id or 0,
        owner_id=deployment.owner_id,
        owner_repo=deployment.owner_repo,
        tag=deployment.tag,
        app_port=deployment.app_port,
        deploy_path=deployment.deploy_path,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
    )


@router.get("/{owner_repo:path}/env")
def get_env(
    owner_repo: str,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    validate_owner_repo(owner_repo)
    deployment = get_deployment_for_repo(session, owner_repo, current_user)
    env_path = Path(deployment.deploy_path) / ".env"
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    return {"owner_repo": deployment.owner_repo, "env": parse_env(content), "raw": content}


@router.put("/{owner_repo:path}/env")
def replace_env(
    owner_repo: str,
    body: EnvReplaceRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    validate_owner_repo(owner_repo)
    deployment = get_deployment_for_repo(session, owner_repo, current_user)
    env_path = Path(deployment.deploy_path) / ".env"
    write_env_file(env_path, body.content)
    return {"status": "ok"}


@router.patch("/{owner_repo:path}/env")
def patch_env(
    owner_repo: str,
    body: EnvPatchRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    validate_owner_repo(owner_repo)
    deployment = get_deployment_for_repo(session, owner_repo, current_user)

    env_path = Path(deployment.deploy_path) / ".env"
    existing_content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_values = parse_env(existing_content)
    env_values.update(body.values)
    write_env_file(env_path, dump_env(env_values))
    return {"status": "ok"}


@router.post("/{owner_repo:path}/apply")
def apply_deployment(
    owner_repo: str,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    validate_owner_repo(owner_repo)
    deployment = get_deployment_for_repo(session, owner_repo, current_user)

    compose_path = Path(deployment.deploy_path) / "docker-compose.yml"
    if not compose_path.exists():
        raise HTTPException(status_code=404, detail="docker-compose.yml не найден")
    try:
        owner = session.get(User, deployment.owner_id)
        if not owner:
            raise HTTPException(status_code=500, detail="Владелец деплоя не найден")
        docker_compose_apply(
            compose_path,
            docker_config_dir=get_docker_config_dir_for_user(owner),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Не удалось применить деплой: {exc}"
        ) from exc
    return {"status": "ok"}


@router.delete("/{owner_repo:path}")
def delete_deployment(
    owner_repo: str,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    validate_owner_repo(owner_repo)
    deployment = get_deployment_for_repo(session, owner_repo, current_user)

    linked_dbs = session.exec(
        select(DatabaseInstance).where(DatabaseInstance.deployment_id == deployment.id)
    ).all()
    if linked_dbs:
        raise HTTPException(
            status_code=409,
            detail="Сначала удалите связанные базы данных для этого деплоя",
        )

    deploy_path = Path(deployment.deploy_path)
    compose_path = deploy_path / "docker-compose.yml"
    if compose_path.exists():
        try:
            owner = session.get(User, deployment.owner_id)
            if not owner:
                raise HTTPException(status_code=500, detail="Владелец деплоя не найден")
            docker_compose_down(
                compose_path,
                remove_volumes=True,
                docker_config_dir=get_docker_config_dir_for_user(owner),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Не удалось остановить деплой: {exc}"
            ) from exc

    if deploy_path.exists():
        shutil.rmtree(deploy_path, ignore_errors=True)

    session.delete(deployment)
    session.commit()
    return {"status": "ok"}
