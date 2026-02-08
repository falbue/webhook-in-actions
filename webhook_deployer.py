import hashlib
import hmac
import os
import subprocess
from pathlib import Path
from flask import Flask, request, abort

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
DEPLOY_ROOT = Path(os.environ.get("DEPLOY_ROOT", "/apps"))


def get_port_for_repo(owner: str, repo_name: str) -> int:
    """Рассчитывает внешний порт для репозитория."""
    # Получаем список всех владельцев (папок в DEPLOY_ROOT)
    owners = sorted([d.name for d in DEPLOY_ROOT.iterdir() if d.is_dir()])

    try:
        owner_index = owners.index(owner)
    except ValueError:
        # Если владелец новый — добавляем его в конец
        owner_index = len(owners)

    base_port = 2000 + owner_index * 1000

    # Получаем список проектов этого владельца
    owner_path = DEPLOY_ROOT / owner
    if owner_path.exists():
        repos = sorted([d.name for d in owner_path.iterdir() if d.is_dir()])
    else:
        repos = []

    try:
        repo_index = repos.index(repo_name)
    except ValueError:
        # Новый проект — добавляем в конец
        repo_index = len(repos)

    # Порт начинается с 1 (2001, 2002, ...)
    port = base_port + repo_index + 1
    return port


def verify_signature(payload: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET or not sig_header:
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def ensure_compose_file(repo_path: Path, full_repo_name: str):
    compose_file = repo_path / "docker-compose.yml"
    if compose_file.exists():
        return

    owner, repo_name = full_repo_name.split("/", 1)
    external_port = get_port_for_repo(owner, repo_name)

    compose_content = f"""version: '3.8'
services:
  app:
    image: ghcr.io/{full_repo_name}:latest
    env_file:
      - .env
    environment:
      - IN_DOCKER=1
    ports:
      - "{external_port}:5000"   # ← Внешний порт : внутренний порт приложения
    restart: unless-stopped
"""
    compose_file.write_text(compose_content, encoding="utf-8")
    print(f"✅ Создан docker-compose.yml для {full_repo_name} на порту {external_port}")


@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Hub-Signature-256")
    payload = request.get_data()
    if not verify_signature(payload, sig):  # type: ignore
        abort(403)

    try:
        data = request.get_json()
        full_repo = data["repo"]
        tag = data["tag"]  # noqa: F841
    except (KeyError, TypeError):
        abort(400)

    if (
        not full_repo
        or full_repo.count("/") != 1
        or not all(c.isalnum() or c in "-_./" for c in full_repo)
    ):
        abort(400)

    owner, repo_name = full_repo.split("/", 1)
    repo_path = DEPLOY_ROOT / owner / repo_name
    repo_path.mkdir(parents=True, exist_ok=True)

    ensure_compose_file(repo_path, full_repo)

    try:
        os.chdir(repo_path)
        subprocess.run(["docker", "compose", "pull"], check=True, capture_output=True)
        subprocess.run(
            ["docker", "compose", "up", "-d", "--remove-orphans"],
            check=True,
            capture_output=True,
        )
        return "Deployed", 200
    except subprocess.CalledProcessError as e:
        print(f"💥 Ошибка: {e.stderr.decode()}")
        return "Deploy failed", 500
