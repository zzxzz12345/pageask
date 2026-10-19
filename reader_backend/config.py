from dataclasses import dataclass, field
from pathlib import Path
import os
import socket
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env.local', override=False)


def reader_allowed_hosts():
    hostname = socket.gethostname().lower().rstrip('.')
    shortname = hostname.split('.')[0]
    hosts = ['127.0.0.1', 'localhost', 'testserver', hostname, shortname, f'{shortname}.local']
    try:
        hosts.extend(sorted({item[4][0] for item in socket.getaddrinfo(
            hostname, None, socket.AF_INET, socket.SOCK_STREAM)}))
    except OSError:
        pass  # 主机名暂时无法解析时，仍可通过本机地址及配置的域名访问。
    hosts.extend(host.strip().lower() for host in os.getenv('READER_ALLOWED_HOSTS', '').split(','))
    return tuple(dict.fromkeys(host for host in hosts if host))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv('READER_DATA_DIR', str(ROOT / '.reader-data')))
    api_key: str = field(default=os.getenv('COMMANDCODE_API_KEY', ''), repr=False)
    base_url: str = os.getenv('COMMANDCODE_BASE_URL', 'https://api.commandcode.ai/provider/v1')
    model: str = os.getenv('READER_MODEL', 'deepseek/deepseek-v4.1-flash')
    effort: str = os.getenv('READER_EFFORT', 'none')
    allowed_hosts: tuple[str, ...] = field(default_factory=reader_allowed_hosts)
    request_timeout: float = 100
    run_timeout: float = 360
    max_turns: int = 24
    max_images: int = 3

POLICY_VERSION = 'grounded-sparse-v5'
DELIVERY_VERSION = 'progressive-v1'
MODELS = ('deepseek/deepseek-v4.1-flash', 'xiaomi/mimo-v2.6-flash')
