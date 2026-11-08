#!/usr/bin/env bash
# Оновити код Лін на сервері з поточного коміту і перезапустити хаб.
#   bash cloud/deploy.sh ubuntu@адреса-сервера
# Код лягає в /home/lin/app власником root (служба його лише читає), дані й ключі
# в /home/lin/data не чіпаються.
set -euo pipefail
host="${1:?вкажи ubuntu@адреса-сервера}"
cd "$(dirname "$0")/.."
git -c core.autocrlf=false archive --format=tar HEAD cloud raphael/__init__.py raphael/settings.py raphael/tg.py raphael/emotion.py raphael/news.py raphael/mail_triage.py |
  ssh "$host" 'set -e
    rm -rf /tmp/lin-app && mkdir /tmp/lin-app && tar -x -C /tmp/lin-app
    sudo rsync -a --delete /tmp/lin-app/ /home/lin/app/ && rm -rf /tmp/lin-app
    sudo chown -R root:lin /home/lin/app && sudo chmod -R u=rwX,g=rX,o= /home/lin/app
    sudo systemctl restart lin-cloud && sleep 3 && systemctl is-active lin-cloud'
