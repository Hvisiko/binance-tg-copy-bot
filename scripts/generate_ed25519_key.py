from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.key_utils import generate_ed25519_keypair  # noqa: E402


def main() -> None:
    pub_pem, _, env_priv = generate_ed25519_keypair()
    print("=" * 68)
    print("🔑 幣安免綁固定 IP 專用 Ed25519 API 金鑰組已產生！")
    print("=" * 68)
    print("\n【步驟 1】請複製下方「公鑰 (Public Key)」貼到幣安後台「建立自生成 API 金鑰」：\n")
    print(pub_pem)
    print("\n" + "-" * 68)
    print("\n【步驟 2】請將下方整行複製填入 Zeabur / 雲端環境變數 BINANCE_ED25519_PRIVATE_KEY：\n")
    print(f"BINANCE_ED25519_PRIVATE_KEY={env_priv}")
    print("\n⚠️ 安全提醒：請勿開啟「提現」權限，並妥善保管私鑰切勿外洩！")
    print("=" * 68)


if __name__ == "__main__":
    main()
