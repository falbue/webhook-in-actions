import re
from pathlib import Path

from fastapi import HTTPException, status
from sqlmodel import Session, select

from deploy_app.config import (
    APP_PORT_OFFSET_END,
    APP_PORT_OFFSET_START,
    DB_PORT_OFFSET_END,
    DB_PORT_OFFSET_START,
    DOCKER_AUTH_ROOT,
    USER_PORT_BLOCK_START,
)
from deploy_app.models import DatabaseInstance, Deployment, User, UserRole


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())


def validate_owner_repo(owner_repo: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", owner_repo):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="owner_repo должен быть в формате owner/repo",
        )


def split_owner_repo(owner_repo: str) -> tuple[str, str]:
    return owner_repo.split("/", 1)


def build_repo_slug(owner_repo: str) -> str:
    _, repo_name = split_owner_repo(owner_repo)
    return slugify(repo_name)


def build_owner_slug(owner_username: str) -> str:
    return slugify(owner_username)


def build_project_name(owner_repo: str, owner_username: str) -> str:
    return f"{build_owner_slug(owner_username)}-{build_repo_slug(owner_repo)}"


def get_deployment_by_owner_repo(session: Session, owner_repo: str) -> Deployment | None:
    return session.exec(select(Deployment).where(Deployment.owner_repo == owner_repo)).first()


def write_env_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def parse_env(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def dump_env(values: dict[str, str]) -> str:
    lines = [f"{key}={value}" for key, value in sorted(values.items())]
    return "\n".join(lines) + ("\n" if lines else "")


def check_deploy_limit(session: Session, user: User) -> None:
    if user.role in (UserRole.PREMIUM, UserRole.ADMIN):
        return
    count = len(
        session.exec(select(Deployment).where(Deployment.owner_id == user.id)).all()
    )
    if count >= 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Лимит достигнут: basic-пользователь может иметь только 1 деплой",
        )


def can_access_deployment(user: User, deployment: Deployment) -> bool:
    return user.role == UserRole.ADMIN or deployment.owner_id == user.id


def get_user_port_block_base(user: User) -> int:
    if user.id is None:
        raise HTTPException(
            status_code=500, detail="Пользователь не имеет идентификатора"
        )
    x = USER_PORT_BLOCK_START + (user.id - 1)
    return x * 1000


def get_app_port_range_for_user(user: User) -> tuple[int, int]:
    base = get_user_port_block_base(user)
    return base + APP_PORT_OFFSET_START, base + APP_PORT_OFFSET_END


def allocate_app_port(session: Session, user: User) -> int:
    start_port, end_port = get_app_port_range_for_user(user)
    used_ports = set(
        session.exec(
            select(Deployment.app_port).where(Deployment.owner_id == user.id)
        ).all()
    )
    for port in range(start_port, end_port + 1):
        if port not in used_ports:
            return port
    raise HTTPException(
        status_code=507,
        detail=f"Свободные APP порты пользователя закончились в диапазоне {start_port}-{end_port}",
    )


def get_db_port_range_for_user(user: User) -> tuple[int, int]:
    base = get_user_port_block_base(user)
    return base + DB_PORT_OFFSET_START, base + DB_PORT_OFFSET_END


def allocate_db_port(session: Session, user: User) -> int:
    start_port, end_port = get_db_port_range_for_user(user)
    used_ports = set(
        session.exec(
            select(DatabaseInstance.host_port).where(
                DatabaseInstance.owner_id == user.id
            )
        ).all()
    )
    for port in range(start_port, end_port + 1):
        if port not in used_ports:
            return port
    raise HTTPException(
        status_code=507,
        detail=f"Свободные DB порты пользователя закончились в диапазоне {start_port}-{end_port}",
    )


def get_docker_config_dir_for_user(user: User) -> Path:
    owner = re.sub(r"[^a-z0-9_-]", "-", user.username.lower())
    return DOCKER_AUTH_ROOT / owner
