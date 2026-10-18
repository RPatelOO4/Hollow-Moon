#!/bin/bash
# Creates the Linux users for Hollow Moon, installs the game to /opt/hollow, and
# prepares the socket directory.  Run once with sudo.   usage: sudo ./setup_users.sh [N_PLAYERS]
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
N=${1:-7}
NAMES=(alice bob carol dave erin frank gina heidi ivan judy kevin laura)
useradd -m -s /bin/bash hw_mod 2>/dev/null || true
for ((i=0; i<N; i++)); do
    useradd -m -s /bin/bash "hw_${NAMES[$i]}" 2>/dev/null || true
done
# game code, readable by everybody (it contains no secrets)
mkdir -p /opt/hollow
cp hollowcomm.py moderator.py player.py bot.py /opt/hollow/
chmod -R a+rX /opt/hollow
# socket directory: only the moderator may create the socket in it; everyone may traverse it
mkdir -p /run/hollow
chown hw_mod:hw_mod /run/hollow
chmod 755 /run/hollow
# moderator log directory: private, because it records every role
mkdir -p /var/log/hollow
chown hw_mod:hw_mod /var/log/hollow
chmod 700 /var/log/hollow
echo "ok: users hw_mod and $(for ((i=0;i<N;i++)); do printf 'hw_%s ' ${NAMES[$i]}; done)"
