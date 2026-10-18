#!/bin/bash
# Removes everything setup_users.sh created.   usage: sudo ./teardown_users.sh
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
for u in $(getent passwd | cut -d: -f1 | grep '^hw_'); do userdel -r "$u" 2>/dev/null; done
rm -rf /opt/hollow /run/hollow /var/log/hollow
echo "removed"
