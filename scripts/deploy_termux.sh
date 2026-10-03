#!/usr/bin/env bash
# Copie le code du PC vers Termux (par SSH) et (ré)installe l'outil sur le téléphone.
#
#   bash scripts/deploy_termux.sh 192.168.1.24
#
# Prérequis côté téléphone : Termux ouvert, `sshd` lancé, clé SSH déposée (voir README).
# Le code est copié dans ~/musique-src et installé dans le venv ~/.venv-musique ;
# la commande `musique` est ensuite disponible directement dans Termux.
set -euo pipefail

IP="${1:?usage: deploy_termux.sh IP_DU_TELEPHONE [CLE_SSH]}"
KEY="${2:-$HOME/.ssh/termux_musique}"
SSH=(ssh -i "$KEY" -p 8022 -o BatchMode=yes -o ConnectTimeout=10 "$IP")
cd "$(dirname "$0")/.."

echo "→ copie du code vers $IP:~/musique-src"
tar --exclude=.venv --exclude=__pycache__ --exclude='*.egg-info' --exclude=.pytest_cache -czf - . \
  | "${SSH[@]}" 'rm -rf ~/musique-src && mkdir -p ~/musique-src && tar -xzf - -C ~/musique-src'

echo "→ installation dans ~/.venv-musique"
"${SSH[@]}" '
  set -e
  [ -d ~/.venv-musique ] || python -m venv ~/.venv-musique
  ~/.venv-musique/bin/pip install -q --disable-pip-version-check -e "$HOME/musique-src[test]"
  ln -sf ~/.venv-musique/bin/musique "$PREFIX/bin/musique"
  musique --version
'
echo "→ fait. Sur le téléphone : musique doctor"
