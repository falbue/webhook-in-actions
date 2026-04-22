import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from deploy_app.config import ENABLE_NGINX_GATEWAY
from deploy_app.db import get_session
from deploy_app.deps import require_auth
from deploy_app.models import Deployment, User, UserRole
from deploy_app.schemas import (
    NginxCertbotRequest,
    NginxCustomConfigRequest,
    NginxPresetApiRequest,
    NginxPresetPreviewRequest,
)
from deploy_app.services.deployments import (
    build_project_name,
    get_deployment_by_owner_repo,
    validate_owner_repo,
)
from deploy_app.services.docker_ops import (
    docker_compose_run_certbot,
    docker_compose_up_no_pull,
    ensure_gateway_stack,
    validate_nginx_config,
)

router = APIRouter(prefix="/deployments", tags=["nginx"])


def ensure_nginx_gateway_enabled() -> None:
    if not ENABLE_NGINX_GATEWAY:
        raise HTTPException(
            status_code=409,
            detail=(
                "Встроенный Nginx gateway отключен через ENABLE_NGINX_GATEWAY=false"
            ),
        )


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())


def get_deployment_for_nginx(
    session: Session,
    owner_repo: str,
    current_user: User,
) -> tuple[Deployment, User]:
    deployment = get_deployment_by_owner_repo(session, owner_repo)
    if not deployment:
        raise HTTPException(status_code=404, detail="Деплой не найден")
    if current_user.role != UserRole.ADMIN and deployment.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нет доступа к деплою")

    owner = session.get(User, deployment.owner_id)
    if not owner:
        raise HTTPException(status_code=500, detail="Владелец деплоя не найден")
    return deployment, owner


def conf_file_path(compose_path: Path, owner_repo: str, domain: str) -> Path:
    conf_name = f"dpl-{slugify(owner_repo)}-{slugify(domain)}.conf"
    return compose_path.parent / "conf.d" / conf_name


def write_config_with_validation(conf_path: Path, content: str, compose_path: Path) -> None:
    old_content = conf_path.read_text(encoding="utf-8") if conf_path.exists() else None
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(content, encoding="utf-8")

    try:
        validate_nginx_config(compose_path.parent)
    except Exception as exc:
        if old_content is None:
            conf_path.unlink(missing_ok=True)
        else:
            conf_path.write_text(old_content, encoding="utf-8")
        raise HTTPException(status_code=422, detail=f"Nginx config invalid: {exc}") from exc


def render_api_preset_config(
    domain: str,
    app_host: str,
    app_port: int,
    use_ssl: bool,
    force_https: bool,
) -> str:
    acme_block = """
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
"""
    proxy_block = f"""
    location / {{
        proxy_pass http://{app_host}:{app_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""

    if not use_ssl:
        return f"""server {{
    listen 80;
    server_name {domain};
{acme_block}{proxy_block}}}
"""

    https_redirect = ""
    if force_https:
        https_redirect = """
    location / {
        return 301 https://$host$request_uri;
    }
"""

    return f"""server {{
    listen 80;
    server_name {domain};
{acme_block}{https_redirect}}}

server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

{proxy_block}}}
"""


@router.post("/{owner_repo:path}/nginx/preset-api")
def set_nginx_preset_api(
    owner_repo: str,
    body: NginxPresetApiRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    ensure_nginx_gateway_enabled()
    validate_owner_repo(owner_repo)
    deployment, owner = get_deployment_for_nginx(session, owner_repo, current_user)
    compose_path = ensure_gateway_stack()

    project_name = build_project_name(deployment.owner_repo, owner.username)
    app_host = f"{project_name}-app-1"
    config = render_api_preset_config(
        domain=body.domain,
        app_host=app_host,
        app_port=5000,
        use_ssl=False,
        force_https=body.force_https,
    )

    conf_path = conf_file_path(compose_path, owner_repo, body.domain)
    write_config_with_validation(conf_path, config, compose_path)

    docker_compose_up_no_pull(compose_path)
    return {"status": "ok", "config_path": str(conf_path)}


@router.post("/{owner_repo:path}/nginx/preview/preset-api")
def preview_nginx_preset_api(
    owner_repo: str,
    body: NginxPresetPreviewRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    validate_owner_repo(owner_repo)
    deployment, owner = get_deployment_for_nginx(session, owner_repo, current_user)
    project_name = build_project_name(deployment.owner_repo, owner.username)
    app_host = f"{project_name}-app-1"
    config = render_api_preset_config(
        domain=body.domain,
        app_host=app_host,
        app_port=5000,
        use_ssl=body.use_ssl,
        force_https=body.force_https,
    )
    return {"config": config}


@router.put("/{owner_repo:path}/nginx/custom")
def set_nginx_custom_config(
    owner_repo: str,
    body: NginxCustomConfigRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    ensure_nginx_gateway_enabled()
    validate_owner_repo(owner_repo)
    get_deployment_for_nginx(session, owner_repo, current_user)
    compose_path = ensure_gateway_stack()

    conf_path = conf_file_path(compose_path, owner_repo, body.domain)
    write_config_with_validation(conf_path, body.content.strip() + "\n", compose_path)

    docker_compose_up_no_pull(compose_path)
    return {"status": "ok", "config_path": str(conf_path)}


@router.post("/{owner_repo:path}/nginx/certbot")
def activate_certbot(
    owner_repo: str,
    body: NginxCertbotRequest,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    ensure_nginx_gateway_enabled()
    validate_owner_repo(owner_repo)
    deployment, owner = get_deployment_for_nginx(session, owner_repo, current_user)
    compose_path = ensure_gateway_stack()

    project_name = build_project_name(deployment.owner_repo, owner.username)
    app_host = f"{project_name}-app-1"

    pre_config = render_api_preset_config(
        domain=body.domain,
        app_host=app_host,
        app_port=5000,
        use_ssl=False,
        force_https=False,
    )
    conf_path = conf_file_path(compose_path, owner_repo, body.domain)
    write_config_with_validation(conf_path, pre_config, compose_path)
    docker_compose_up_no_pull(compose_path)

    docker_compose_run_certbot(
        compose_path=compose_path,
        domain=body.domain,
        email=body.email,
        staging=body.staging,
    )

    ssl_config = render_api_preset_config(
        domain=body.domain,
        app_host=app_host,
        app_port=5000,
        use_ssl=True,
        force_https=True,
    )
    write_config_with_validation(conf_path, ssl_config, compose_path)
    docker_compose_up_no_pull(compose_path)

    return {"status": "ok", "config_path": str(conf_path)}


@router.delete("/{owner_repo:path}/nginx")
def delete_nginx_config(
    owner_repo: str,
    domain: str,
    current_user: User = Depends(require_auth),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    ensure_nginx_gateway_enabled()
    validate_owner_repo(owner_repo)
    get_deployment_for_nginx(session, owner_repo, current_user)
    compose_path = ensure_gateway_stack()

    conf_path = conf_file_path(compose_path, owner_repo, domain)
    if conf_path.exists():
        old_content = conf_path.read_text(encoding="utf-8")
        conf_path.unlink()
        try:
            validate_nginx_config(compose_path.parent)
        except Exception as exc:
            conf_path.write_text(old_content, encoding="utf-8")
            raise HTTPException(status_code=422, detail=f"Nginx config invalid: {exc}") from exc

    docker_compose_up_no_pull(compose_path)
    return {"status": "ok"}
