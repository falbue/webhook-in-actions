import hashlib
import hmac
import os
import subprocess
import logging
from pathlib import Path
from flask import Flask, request, abort, jsonify
import base64

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
DEPLOY_ROOT = Path(os.environ.get("DEPLOY_ROOT", "/apps"))


def get_port_for_repo(owner: str, repo_name: str) -> int:
    """Рассчитывает внешний порт для репозитория (стабильный через сортировку)."""
    owners = sorted([d.name for d in DEPLOY_ROOT.iterdir() if d.is_dir()])
    owner_index = owners.index(owner) if owner in owners else len(owners)
    base_port = 2000 + owner_index * 1000

    owner_path = DEPLOY_ROOT / owner
    repos = (
        sorted([d.name for d in owner_path.iterdir() if d.is_dir()])
        if owner_path.exists()
        else []
    )
    repo_index = repos.index(repo_name) if repo_name in repos else len(repos)

    return base_port + repo_index + 1  # Порт начинается с 1


def verify_signature(payload: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET or not sig_header:
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


@app.route("/webhook", methods=["POST"])
def webhook():
    # === Валидация подписи — без изменений ===
    sig = request.headers.get("X-Hub-Signature-256")
    payload = request.get_data()
    if not verify_signature(payload, sig):  # type: ignore
        logger.warning("❌ Неверная подпись вебхука")
        abort(403, description="Invalid signature")

    # === Парсинг данных ===
    try:
        data = request.get_json()
        if not data:
            abort(400, description="Empty payload")
        full_repo = data.get("repo", "").strip()
        tag = data.get("tag", "").strip()
        compose_b64 = data.get("compose_b64", "").strip()
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга JSON: {e}")
        abort(400, description="Invalid JSON")

    if not full_repo or full_repo.count("/") != 1:
        abort(400, description="Invalid repo format")
    if not tag:
        abort(400, description="Tag is required")

    owner, repo_name = full_repo.split("/", 1)
    repo_path = DEPLOY_ROOT / owner / repo_name
    repo_path.mkdir(parents=True, exist_ok=True)

    compose_file = repo_path / "docker-compose.yml"

    # === Используем переданный compose или fallback ===
    if compose_b64:
        try:
            compose_content = base64.b64decode(compose_b64).decode("utf-8")
            compose_file.write_text(compose_content, encoding="utf-8")
            logger.info(
                f"✅ docker-compose.yml получен из вебхука для {full_repo}:{tag}"
            )
        except Exception as e:
            logger.exception(f"💥 Ошибка декодирования compose_b64: {e}")
            return jsonify({"error": "Invalid compose_b64"}), 400
    else:
        return abort(400, description="docker-compose.yml не найден")

    # === Запуск docker compose — БЕЗ ИЗМЕНЕНИЙ ===
    try:
        logger.info(f"🔄 Запуск деплоя {full_repo}:{tag} в {repo_path}")

        pull = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "pull"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if pull.returncode != 0:
            logger.error(f"❌ docker compose pull failed:\n{pull.stderr}")
            return jsonify({"error": "Pull failed", "stderr": pull.stderr}), 500

        up = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "up",
                "-d",
                "--remove-orphans",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if up.returncode != 0:
            logger.error(f"❌ docker compose up failed:\n{up.stderr}")
            return jsonify({"error": "Deploy failed", "stderr": up.stderr}), 500

        logger.info(f"✅ Успешный деплой {full_repo}:{tag}")
        return jsonify({"status": "success", "repo": full_repo, "tag": tag}), 200

    except subprocess.TimeoutExpired:
        logger.exception("💥 Таймаут выполнения docker compose")
        return jsonify({"error": "Deployment timeout"}), 500
    except Exception as e:
        logger.exception(f"💥 Критическая ошибка деплоя: {e}")
        return jsonify({"error": "Internal server error"}), 500
